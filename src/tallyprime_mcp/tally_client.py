"""Read-only client for TallyPrime's XML-over-HTTP interface.

Every request built here is a TALLYREQUEST=Export. Nothing in this module
can create, alter or delete data in Tally.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from xml.sax.saxutils import escape

import httpx

DEFAULT_URL = "http://localhost:9000"


class TallyError(Exception):
    """A problem talking to Tally, phrased so an end user can act on it."""


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

# Tally emits character references such as &#4; that are illegal in XML 1.0.
_BAD_CHAR_REF = re.compile(r"&#(x[0-9a-fA-F]+|\d+);")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _legal_xml_codepoint(cp: int) -> bool:
    return cp in (0x9, 0xA, 0xD) or 0x20 <= cp <= 0xD7FF or 0xE000 <= cp <= 0xFFFD or cp >= 0x10000


def sanitize_xml(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        ref = match.group(1)
        cp = int(ref[1:], 16) if ref.lower().startswith("x") else int(ref)
        return match.group(0) if _legal_xml_codepoint(cp) else ""

    return _CONTROL_CHARS.sub("", _BAD_CHAR_REF.sub(repl, text))


def decode_response(raw: bytes) -> str:
    """Tally answers in UTF-8 or UTF-16 depending on the request headers."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    if len(raw) > 3 and raw[1:2] == b"\x00" and raw[3:4] == b"\x00":
        return raw.decode("utf-16-le")
    return raw.decode("utf-8", errors="replace").lstrip("﻿")


def parse_amount(text: str | None) -> float:
    """Parse a Tally amount. Returns 0.0 for blanks.

    Tally's sign convention is kept as-is: debit is negative, credit positive.
    Also accepts display forms like "1,200.00 Dr".
    """
    if not text:
        return 0.0
    s = text.strip().replace(",", "")
    if not s:
        return 0.0
    suffix = None
    lowered = s.lower()
    if lowered.endswith("dr"):
        suffix, s = "dr", s[:-2].strip()
    elif lowered.endswith("cr"):
        suffix, s = "cr", s[:-2].strip()
    match = re.search(r"-?\d+(?:\.\d+)?", s)
    if not match:
        return 0.0
    value = float(match.group(0))
    if suffix == "dr":
        return -abs(value)
    if suffix == "cr":
        return abs(value)
    return value


def parse_tally_date(text: str | None) -> date | None:
    """Tally exports dates as YYYYMMDD; reports sometimes use 1-Apr-2024."""
    if not text:
        return None
    s = text.strip()
    for fmt in ("%Y%m%d", "%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_user_date(text: str) -> date:
    """Dates coming from the model: ISO (2024-04-01) or YYYYMMDD."""
    parsed = parse_tally_date(text)
    if parsed is None:
        raise TallyError(f"Could not read the date '{text}'. Use YYYY-MM-DD, e.g. 2024-04-01.")
    return parsed


def _text(node: ET.Element, tag: str) -> str:
    child = node.find(tag)
    return (child.text or "").strip() if child is not None else ""


def _name(node: ET.Element) -> str:
    """Object names arrive as a NAME attribute, a NAME child, or both."""
    return (node.get("NAME") or _text(node, "NAME")).strip()


# --------------------------------------------------------------------------
# Request building
# --------------------------------------------------------------------------


def build_collection_request(
    collection_name: str,
    object_type: str,
    fetch: list[str],
    *,
    company: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    child_of: str | None = None,
) -> str:
    """An Export request carrying its own inline TDL collection definition,
    so it works on a stock TallyPrime with no custom TDL installed."""
    static = ["<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"]
    if company:
        static.append(f"<SVCURRENTCOMPANY>{escape(company)}</SVCURRENTCOMPANY>")
    if from_date:
        static.append(f'<SVFROMDATE TYPE="Date">{from_date:%Y%m%d}</SVFROMDATE>')
    if to_date:
        static.append(f'<SVTODATE TYPE="Date">{to_date:%Y%m%d}</SVTODATE>')

    body = [f"<TYPE>{escape(object_type)}</TYPE>"]
    if child_of:
        body.append(f"<CHILDOF>{escape(child_of)}</CHILDOF>")
        body.append("<BELONGSTO>Yes</BELONGSTO>")
    body.append(f"<FETCH>{', '.join(fetch)}</FETCH>")

    return (
        "<ENVELOPE>"
        "<HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST>"
        f"<TYPE>Collection</TYPE><ID>{escape(collection_name)}</ID></HEADER>"
        "<BODY><DESC>"
        f"<STATICVARIABLES>{''.join(static)}</STATICVARIABLES>"
        "<TDL><TDLMESSAGE>"
        f'<COLLECTION NAME="{escape(collection_name)}" ISMODIFY="No">{"".join(body)}</COLLECTION>'
        "</TDLMESSAGE></TDL>"
        "</DESC></BODY></ENVELOPE>"
    )


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


@dataclass
class TallyConfig:
    url: str = DEFAULT_URL
    company: str | None = None
    timeout: float = 60.0

    @classmethod
    def from_env(cls) -> "TallyConfig":
        return cls(
            url=os.environ.get("TALLY_URL", DEFAULT_URL).rstrip("/"),
            company=os.environ.get("TALLY_COMPANY") or None,
            timeout=float(os.environ.get("TALLY_TIMEOUT", "60")),
        )


class TallyClient:
    def __init__(self, config: TallyConfig | None = None):
        self.config = config or TallyConfig.from_env()

    # -- transport ---------------------------------------------------------

    async def _post(self, xml_request: str) -> ET.Element:
        if "<TALLYREQUEST>Export</TALLYREQUEST>" not in xml_request:
            raise TallyError("Refusing to send a non-Export request: this connector is read-only.")
        try:
            async with httpx.AsyncClient(timeout=self.config.timeout, trust_env=False) as http:
                response = await http.post(
                    self.config.url,
                    content=xml_request.encode("utf-8"),
                    headers={"Content-Type": "text/xml; charset=utf-8"},
                )
        except httpx.ConnectError as exc:
            raise TallyError(
                f"Could not reach TallyPrime at {self.config.url}. Check that TallyPrime is open, "
                "a company is loaded, and the HTTP server is enabled "
                "(F1 Help > Settings > Advanced Configuration, port 9000)."
            ) from exc
        except httpx.TimeoutException as exc:
            raise TallyError(
                f"TallyPrime did not answer within {self.config.timeout:.0f}s. "
                "Try a shorter date range, or raise TALLY_TIMEOUT."
            ) from exc
        except httpx.HTTPError as exc:
            raise TallyError(f"Network error talking to TallyPrime: {exc}") from exc

        text = sanitize_xml(decode_response(response.content))
        if not text.strip():
            raise TallyError("TallyPrime returned an empty response. Is a company loaded?")
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise TallyError(f"TallyPrime returned XML that could not be parsed: {exc}") from exc

        error = root.find(".//LINEERROR")
        if error is not None and (error.text or "").strip():
            raise TallyError(f"TallyPrime reported: {error.text.strip()}")
        return root

    def _company(self, company: str | None) -> str | None:
        return company or self.config.company

    # -- queries -----------------------------------------------------------

    async def list_companies(self) -> list[dict]:
        root = await self._post(
            build_collection_request("MCP Companies", "Company", ["NAME", "STARTINGFROM", "BOOKSFROM"])
        )
        out = []
        for node in root.iter("COMPANY"):
            name = _name(node)
            if not name:
                continue
            start = parse_tally_date(_text(node, "STARTINGFROM"))
            books = parse_tally_date(_text(node, "BOOKSFROM"))
            out.append(
                {
                    "name": name,
                    "financial_year_from": start.isoformat() if start else None,
                    "books_from": books.isoformat() if books else None,
                }
            )
        return out

    async def list_ledgers(self, company: str | None = None, group: str | None = None) -> list[dict]:
        root = await self._post(
            build_collection_request(
                "MCP Ledgers",
                "Ledger",
                ["NAME", "PARENT", "OPENINGBALANCE", "CLOSINGBALANCE"],
                company=self._company(company),
                child_of=group,
            )
        )
        out = []
        for node in root.iter("LEDGER"):
            name = _name(node)
            if not name:
                continue
            out.append(
                {
                    "name": name,
                    "group": _text(node, "PARENT"),
                    "opening_balance": parse_amount(_text(node, "OPENINGBALANCE")),
                    "closing_balance": parse_amount(_text(node, "CLOSINGBALANCE")),
                }
            )
        return out

    async def get_vouchers(
        self, from_date: date, to_date: date, company: str | None = None
    ) -> list[dict]:
        if to_date < from_date:
            raise TallyError("to_date is before from_date.")
        root = await self._post(
            build_collection_request(
                "MCP Vouchers",
                "Voucher",
                ["DATE", "VOUCHERNUMBER", "VOUCHERTYPENAME", "PARTYLEDGERNAME", "AMOUNT", "NARRATION"],
                company=self._company(company),
                from_date=from_date,
                to_date=to_date,
            )
        )
        out = []
        for node in root.iter("VOUCHER"):
            when = parse_tally_date(_text(node, "DATE"))
            # Collections do not always honour SVFROMDATE/SVTODATE, so the
            # range is enforced here as well.
            if when is None or not (from_date <= when <= to_date):
                continue
            out.append(
                {
                    "date": when.isoformat(),
                    "voucher_type": _text(node, "VOUCHERTYPENAME") or (node.get("VCHTYPE") or ""),
                    "voucher_number": _text(node, "VOUCHERNUMBER"),
                    "party": _text(node, "PARTYLEDGERNAME"),
                    "amount": abs(parse_amount(_text(node, "AMOUNT"))),
                    "narration": _text(node, "NARRATION"),
                }
            )
        out.sort(key=lambda v: (v["date"], v["voucher_number"]))
        return out

    async def get_pending_bills(self, company: str | None = None) -> list[dict]:
        root = await self._post(
            build_collection_request(
                "MCP Bills",
                "Bills",
                ["NAME", "PARENT", "BILLDATE", "CLOSINGBALANCE", "BILLCREDITPERIOD"],
                company=self._company(company),
            )
        )
        out = []
        for node in root.iter("BILL"):
            balance = parse_amount(_text(node, "CLOSINGBALANCE"))
            if balance == 0:
                continue
            bill_date = parse_tally_date(_text(node, "BILLDATE"))
            due_date = parse_tally_date(_text(node, "BILLCREDITPERIOD"))
            out.append(
                {
                    "bill_ref": _name(node),
                    "party": _text(node, "PARENT"),
                    "bill_date": bill_date.isoformat() if bill_date else None,
                    "due_date": due_date.isoformat() if due_date else None,
                    # Debit (negative) balance = money owed to the business.
                    "kind": "receivable" if balance < 0 else "payable",
                    "amount": abs(balance),
                }
            )
        return out
