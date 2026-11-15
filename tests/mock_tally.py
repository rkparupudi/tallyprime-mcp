"""A small stand-in for TallyPrime's HTTP server, for tests and demos.

It answers the collection requests this connector sends, using the response
shape TallyPrime uses for collection exports, including two real-world
quirks: illegal character references (&#4;) and vouchers outside the
requested date range.

Run standalone:  python tests/mock_tally.py 9000
"""

from __future__ import annotations

import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

COMPANIES = """<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>1</STATUS></HEADER><BODY><DESC></DESC><DATA><COLLECTION>
<COMPANY NAME="Sharma Traders Pvt Ltd" RESERVEDNAME="">
 <NAME TYPE="String">Sharma Traders Pvt Ltd</NAME>
 <STARTINGFROM TYPE="Date">20260401</STARTINGFROM>
 <BOOKSFROM TYPE="Date">20240401</BOOKSFROM>
</COMPANY>
</COLLECTION></DATA></BODY></ENVELOPE>"""

LEDGER_ROWS = [
    # name, parent, opening, closing   (debit negative, credit positive)
    ("Cash", "Cash-in-Hand", "-50000.00", "-82500.00"),
    ("HDFC Bank", "Bank Accounts", "-400000.00", "-615250.50"),
    ("Mehta & Sons", "Sundry Debtors", "0.00", "-236000.00"),
    ("Kaveri Retail", "Sundry Debtors", "0.00", "-59000.00"),
    ("Deccan Supplies", "Sundry Creditors", "0.00", "141600.00"),
    ("Sales - GST 18%", "Sales Accounts", "0.00", "950000.00"),
    ("Purchases - GST 18%", "Purchase Accounts", "0.00", "-420000.00"),
    ("Rent", "Indirect Expenses", "0.00", "-180000.00"),
    ("Capital Account", "Capital Account", "450000.00", "501150.50"),
    ("Dormant Ledger", "Sundry Debtors", "0.00", ""),
]

VOUCHER_ROWS = [
    # date, type, number, party, amount, narration
    ("20260930", "Sales", "S-099", "Kaveri Retail", "-11800.00", "Out of range on purpose"),
    ("20261003", "Sales", "S-101", "Mehta & Sons", "-118000.00", "Invoice for October lot&#4;"),
    ("20261007", "Purchase", "P-044", "Deccan Supplies", "141600.00", "Raw material"),
    ("20261012", "Receipt", "R-031", "Kaveri Retail", "-59000.00", "NEFT received"),
    ("20261020", "Sales", "S-102", "Kaveri Retail", "-59000.00", ""),
    ("20261101", "Payment", "PM-012", "Rent", "30000.00", "November rent"),
]

BILL_ROWS = [
    # ref, party, bill date, closing, due date
    ("S-071", "Mehta & Sons", "20260715", "-118000.00", "20260814"),
    ("S-101", "Mehta & Sons", "20261003", "-118000.00", "20261102"),
    ("S-102", "Kaveri Retail", "20261020", "-59000.00", "20261119"),
    ("P-044", "Deccan Supplies", "20261007", "141600.00", "20261106"),
    ("S-050", "Kaveri Retail", "20260501", "0.00", "20260531"),
]


def _esc(s: str) -> str:
    # Leave deliberate character references (&#4;) intact.
    return re.sub(r"&(?!#)", "&amp;", s)


def _wrap(inner: str) -> str:
    return (
        "<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>1</STATUS></HEADER>"
        f"<BODY><DESC></DESC><DATA><COLLECTION>{inner}</COLLECTION></DATA></BODY></ENVELOPE>"
    )


def ledgers_xml(child_of: str | None) -> str:
    rows = [r for r in LEDGER_ROWS if not child_of or r[1] == child_of]
    return _wrap(
        "".join(
            f'<LEDGER NAME="{_esc(n)}" RESERVEDNAME="">'
            f'<PARENT TYPE="String">{_esc(p)}</PARENT>'
            f'<OPENINGBALANCE TYPE="Amount">{o}</OPENINGBALANCE>'
            f'<CLOSINGBALANCE TYPE="Amount">{c}</CLOSINGBALANCE></LEDGER>'
            for n, p, o, c in rows
        )
    )


def vouchers_xml() -> str:
    return _wrap(
        "".join(
            f'<VOUCHER REMOTEID="x{i}" VCHTYPE="{t}" ACTION="Create">'
            f'<DATE TYPE="Date">{d}</DATE>'
            f'<VOUCHERTYPENAME TYPE="String">{t}</VOUCHERTYPENAME>'
            f'<VOUCHERNUMBER TYPE="String">{n}</VOUCHERNUMBER>'
            f'<PARTYLEDGERNAME TYPE="String">{_esc(p)}</PARTYLEDGERNAME>'
            f'<AMOUNT TYPE="Amount">{a}</AMOUNT>'
            f'<NARRATION TYPE="String">{_esc(nar)}</NARRATION></VOUCHER>'
            for i, (d, t, n, p, a, nar) in enumerate(VOUCHER_ROWS)
        )
    )


def bills_xml() -> str:
    return _wrap(
        "".join(
            f'<BILL NAME="{ref}" RESERVEDNAME="">'
            f'<PARENT TYPE="String">{_esc(p)}</PARENT>'
            f'<BILLDATE TYPE="Date">{bd}</BILLDATE>'
            f'<CLOSINGBALANCE TYPE="Amount">{c}</CLOSINGBALANCE>'
            f'<BILLCREDITPERIOD TYPE="Due Date">{dd}</BILLCREDITPERIOD></BILL>'
            for ref, p, bd, c, dd in BILL_ROWS
        )
    )


class Handler(BaseHTTPRequestHandler):
    requests_seen: list[str] = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8")
        Handler.requests_seen.append(body)
        obj_type = re.search(r"<COLLECTION[^>]*>\s*<TYPE>([^<]+)</TYPE>", body)
        kind = obj_type.group(1) if obj_type else ""
        if kind == "Company":
            out = COMPANIES
        elif kind == "Ledger":
            child = re.search(r"<CHILDOF>([^<]+)</CHILDOF>", body)
            out = ledgers_xml(child.group(1).replace("&amp;", "&") if child else None)
        elif kind == "Voucher":
            out = vouchers_xml()
        elif kind == "Bills":
            out = bills_xml()
        else:
            out = "<RESPONSE><LINEERROR>Could not find Report or Collection</LINEERROR></RESPONSE>"
        payload = out.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # keep test output quiet
        pass


def start(port: int = 0) -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9000
    srv, url = start(port)
    print(f"Mock Tally listening on {url}  (Ctrl+C to stop)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.shutdown()
