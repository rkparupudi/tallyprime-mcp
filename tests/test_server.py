import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import mock_tally  # noqa: E402

from tallyprime_mcp import server  # noqa: E402
from tallyprime_mcp.tally_client import (  # noqa: E402
    TallyClient,
    TallyConfig,
    TallyError,
    build_collection_request,
    decode_response,
    parse_amount,
    parse_tally_date,
    sanitize_xml,
)


@pytest.fixture(scope="module")
def tally_url():
    srv, url = mock_tally.start()
    yield url
    srv.shutdown()


@pytest.fixture(autouse=True)
def use_mock(tally_url, monkeypatch):
    monkeypatch.setattr(server, "_client", TallyClient(TallyConfig(url=tally_url, timeout=5)))
    mock_tally.Handler.requests_seen.clear()


# ---- parsing --------------------------------------------------------------


def test_parse_amount_variants():
    assert parse_amount("-13240.00") == -13240.0
    assert parse_amount("1,52,629.50") == 152629.5
    assert parse_amount("1200.00 Dr") == -1200.0
    assert parse_amount("1200.00 Cr") == 1200.0
    assert parse_amount("") == 0.0
    assert parse_amount(None) == 0.0


def test_parse_dates():
    assert parse_tally_date("20261003").isoformat() == "2026-10-03"
    assert parse_tally_date("1-Apr-2016").isoformat() == "2016-04-01"
    assert parse_tally_date("garbage") is None


def test_sanitize_drops_illegal_refs_only():
    assert sanitize_xml("<A>x&#4;y &amp; z&#10;</A>") == "<A>xy &amp; z&#10;</A>"


def test_decode_utf16():
    assert decode_response("<A>₹</A>".encode("utf-16")) == "<A>₹</A>"


def test_request_escapes_and_is_export_only():
    xml = build_collection_request("C", "Ledger", ["NAME"], company="A & B <Ltd>", child_of="X&Y")
    assert "A &amp; B &lt;Ltd&gt;" in xml and "X&amp;Y" in xml
    assert "<TALLYREQUEST>Export</TALLYREQUEST>" in xml


async def test_non_export_request_is_refused(tally_url):
    client = TallyClient(TallyConfig(url=tally_url))
    with pytest.raises(TallyError, match="read-only"):
        await client._post("<ENVELOPE><HEADER><TALLYREQUEST>Import</TALLYREQUEST></HEADER></ENVELOPE>")


# ---- tools ----------------------------------------------------------------


async def test_list_companies():
    out = await server.list_companies()
    assert out["ok"]
    assert out["companies"][0]["name"] == "Sharma Traders Pvt Ltd"
    assert out["companies"][0]["financial_year_from"] == "2026-04-01"


async def test_list_ledgers_group_and_sides():
    out = await server.list_ledgers(group="Sundry Debtors", nonzero_only=True)
    assert [l["name"] for l in out["ledgers"]] == ["Mehta & Sons", "Kaveri Retail"]
    assert out["ledgers"][0]["closing"] == {"amount": 236000.0, "side": "debit"}
    assert "<CHILDOF>Sundry Debtors</CHILDOF>" in mock_tally.Handler.requests_seen[-1]


async def test_list_ledgers_name_filter_and_limit():
    out = await server.list_ledgers(name_contains="gst", limit=1)
    assert out["total_matching"] == 2 and out["returned"] == 1
    assert out["ledgers"][0]["name"] == "Sales - GST 18%"


async def test_vouchers_range_enforced_and_sanitized():
    out = await server.get_vouchers("2026-10-01", "2026-10-31")
    assert out["ok"] and out["total_matching"] == 4  # Sep and Nov rows dropped
    assert out["vouchers"][0]["narration"] == "Invoice for October lot"
    assert out["summary_by_type"]["Sales"] == {"count": 2, "total": 177000.0}
    sent = mock_tally.Handler.requests_seen[-1]
    assert "20261001" in sent and "20261031" in sent


async def test_vouchers_filters():
    out = await server.get_vouchers("2026-10-01", "2026-10-31", voucher_type="sales", party_contains="kaveri")
    assert [v["voucher_number"] for v in out["vouchers"]] == ["S-102"]


async def test_vouchers_bad_date():
    out = await server.get_vouchers("October", "2026-10-31")
    assert out["ok"] is False and "YYYY-MM-DD" in out["error"]


async def test_outstanding_receivables_over_60_days():
    out = await server.get_outstanding("receivable", min_days_outstanding=60, as_of="2026-11-14")
    assert out["bill_count"] == 1
    bill = out["bills"][0]
    assert bill["bill_ref"] == "S-071" and bill["days_outstanding"] == 122 and bill["days_overdue"] == 92
    assert out["total_amount"] == 118000.0


async def test_outstanding_all_receivables_ageing():
    out = await server.get_outstanding("receivable", as_of="2026-11-14")
    assert out["total_amount"] == 295000.0  # zero-balance bill excluded
    assert out["ageing_buckets"]["0-30"] == 59000.0
    assert out["ageing_buckets"]["31-60"] == 118000.0
    assert out["ageing_buckets"]["90+"] == 118000.0
    assert out["by_party"][0] == {"party": "Mehta & Sons", "bills": 2, "amount": 236000.0, "oldest_days": 122}


async def test_outstanding_payables():
    out = await server.get_outstanding("payable", as_of="2026-11-14")
    assert out["bill_count"] == 1 and out["bills"][0]["party"] == "Deccan Supplies"


async def test_trial_balance_balances():
    out = await server.get_trial_balance()
    assert out["total_debit"] == out["total_credit"] == 1592750.5
    assert out["balanced"] is True
    debtors = next(g for g in out["groups"] if g["group"] == "Sundry Debtors")
    assert debtors == {"group": "Sundry Debtors", "debit": 295000.0, "credit": 0.0, "ledgers": 2}


async def test_tally_not_running(monkeypatch):
    monkeypatch.setattr(server, "_client", TallyClient(TallyConfig(url="http://127.0.0.1:1", timeout=2)))
    out = await server.list_companies()
    assert out["ok"] is False and "HTTP server is enabled" in out["error"]
