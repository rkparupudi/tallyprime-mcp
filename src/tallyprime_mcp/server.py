"""TallyPrime MCP server (read-only).

Runs on the same machine as TallyPrime and talks to its local HTTP server.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Literal

try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from .tally_client import TallyClient, TallyError, parse_user_date

mcp = _Server(
    "tallyprime",
    instructions=(
        "Read-only access to the books in a locally running TallyPrime. "
        "Amounts are in the company's base currency. Balances are reported as an absolute "
        "amount plus a side ('debit' or 'credit'). Call list_companies first if you are "
        "unsure which company is loaded. This server cannot create or change anything in Tally."
    ),
)

_client = TallyClient()


def _side(balance: float) -> dict:
    """Tally stores debit as negative. Expose amount + side instead."""
    if balance == 0:
        return {"amount": 0.0, "side": None}
    return {"amount": round(abs(balance), 2), "side": "debit" if balance < 0 else "credit"}


def _fail(exc: TallyError) -> dict:
    return {"ok": False, "error": str(exc)}


@mcp.tool()
async def list_companies() -> dict:
    """List the companies currently loaded in TallyPrime. Also works as a
    connection check: an error here means Tally is not reachable."""
    try:
        companies = await _client.list_companies()
    except TallyError as exc:
        return _fail(exc)
    return {
        "ok": True,
        "default_company": _client.config.company,
        "companies": companies,
    }


@mcp.tool()
async def list_ledgers(
    group: str | None = None,
    name_contains: str | None = None,
    nonzero_only: bool = False,
    limit: int = 200,
    company: str | None = None,
) -> dict:
    """List ledgers with opening and closing balances.

    group: restrict to ledgers under this group, including sub-groups
        (e.g. "Sundry Debtors", "Bank Accounts", "Indirect Expenses").
    name_contains: case-insensitive filter on the ledger name.
    nonzero_only: drop ledgers whose closing balance is zero.
    limit: maximum ledgers to return (largest closing balances first).
    company: company name; defaults to the configured or active company.
    """
    try:
        ledgers = await _client.list_ledgers(company=company, group=group)
    except TallyError as exc:
        return _fail(exc)

    if name_contains:
        needle = name_contains.lower()
        ledgers = [l for l in ledgers if needle in l["name"].lower()]
    if nonzero_only:
        ledgers = [l for l in ledgers if l["closing_balance"] != 0]
    ledgers.sort(key=lambda l: abs(l["closing_balance"]), reverse=True)
    total = len(ledgers)
    limit = max(1, min(limit, 2000))
    return {
        "ok": True,
        "total_matching": total,
        "returned": min(total, limit),
        "ledgers": [
            {
                "name": l["name"],
                "group": l["group"],
                "opening": _side(l["opening_balance"]),
                "closing": _side(l["closing_balance"]),
            }
            for l in ledgers[:limit]
        ],
    }


@mcp.tool()
async def get_vouchers(
    from_date: str,
    to_date: str,
    voucher_type: str | None = None,
    party_contains: str | None = None,
    limit: int = 200,
    company: str | None = None,
) -> dict:
    """Fetch vouchers (transactions) in a date range, oldest first.

    from_date / to_date: inclusive, YYYY-MM-DD.
    voucher_type: e.g. "Sales", "Purchase", "Receipt", "Payment", "Journal".
    party_contains: case-insensitive filter on the party ledger name.
    limit: maximum vouchers to return. Totals by type always cover the full
        matching set, so narrow the range or filters if results are truncated.
    """
    try:
        start, end = parse_user_date(from_date), parse_user_date(to_date)
        vouchers = await _client.get_vouchers(start, end, company=company)
    except TallyError as exc:
        return _fail(exc)

    if voucher_type:
        wanted = voucher_type.strip().lower()
        vouchers = [v for v in vouchers if v["voucher_type"].lower() == wanted]
    if party_contains:
        needle = party_contains.lower()
        vouchers = [v for v in vouchers if needle in v["party"].lower()]

    by_type: dict[str, dict] = defaultdict(lambda: {"count": 0, "total": 0.0})
    for v in vouchers:
        bucket = by_type[v["voucher_type"] or "(unknown)"]
        bucket["count"] += 1
        bucket["total"] = round(bucket["total"] + v["amount"], 2)

    total = len(vouchers)
    limit = max(1, min(limit, 2000))
    return {
        "ok": True,
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
        "total_matching": total,
        "returned": min(total, limit),
        "truncated": total > limit,
        "summary_by_type": dict(by_type),
        "vouchers": vouchers[:limit],
    }


@mcp.tool()
async def get_outstanding(
    kind: Literal["receivable", "payable"] = "receivable",
    min_days_outstanding: int = 0,
    party_contains: str | None = None,
    as_of: str | None = None,
    company: str | None = None,
) -> dict:
    """Pending bills: what customers owe (receivable) or what is owed to
    suppliers (payable), with ageing.

    min_days_outstanding: only bills at least this many days old, counted
        from the bill date (e.g. 60 for "older than 60 days").
    as_of: date to age against, YYYY-MM-DD. Defaults to today.

    Only covers ledgers that use bill-wise details in Tally.
    """
    try:
        today = parse_user_date(as_of) if as_of else date.today()
        bills = await _client.get_pending_bills(company=company)
    except TallyError as exc:
        return _fail(exc)

    rows = []
    for bill in bills:
        if bill["kind"] != kind:
            continue
        if party_contains and party_contains.lower() not in bill["party"].lower():
            continue
        days = None
        if bill["bill_date"]:
            days = (today - date.fromisoformat(bill["bill_date"])).days
        if min_days_outstanding > 0 and (days is None or days < min_days_outstanding):
            continue
        overdue = None
        if bill["due_date"]:
            overdue = max(0, (today - date.fromisoformat(bill["due_date"])).days)
        rows.append({**bill, "days_outstanding": days, "days_overdue": overdue})

    rows.sort(key=lambda r: (r["days_outstanding"] is None, -(r["days_outstanding"] or 0)))

    by_party: dict[str, dict] = defaultdict(lambda: {"bills": 0, "amount": 0.0, "oldest_days": 0})
    buckets = {"0-30": 0.0, "31-60": 0.0, "61-90": 0.0, "90+": 0.0, "undated": 0.0}
    for r in rows:
        p = by_party[r["party"]]
        p["bills"] += 1
        p["amount"] = round(p["amount"] + r["amount"], 2)
        p["oldest_days"] = max(p["oldest_days"], r["days_outstanding"] or 0)
        d = r["days_outstanding"]
        key = "undated" if d is None else "0-30" if d <= 30 else "31-60" if d <= 60 else "61-90" if d <= 90 else "90+"
        buckets[key] = round(buckets[key] + r["amount"], 2)

    parties = sorted(
        ({"party": name, **stats} for name, stats in by_party.items()),
        key=lambda p: p["amount"],
        reverse=True,
    )
    return {
        "ok": True,
        "kind": kind,
        "as_of": today.isoformat(),
        "total_amount": round(sum(r["amount"] for r in rows), 2),
        "bill_count": len(rows),
        "ageing_buckets": buckets,
        "by_party": parties,
        "bills": rows[:500],
    }


@mcp.tool()
async def get_trial_balance(company: str | None = None) -> dict:
    """Trial balance built from ledger closing balances, grouped by each
    ledger's parent group, with debit and credit totals.

    'balanced' is false when total debits and credits differ by more than
    one rupee, which usually points at an unposted opening-balance difference.
    """
    try:
        ledgers = await _client.list_ledgers(company=company)
    except TallyError as exc:
        return _fail(exc)

    groups: dict[str, dict] = defaultdict(lambda: {"debit": 0.0, "credit": 0.0, "ledgers": 0})
    total_debit = total_credit = 0.0
    for l in ledgers:
        bal = l["closing_balance"]
        if bal == 0:
            continue
        g = groups[l["group"] or "(ungrouped)"]
        g["ledgers"] += 1
        if bal < 0:
            g["debit"] = round(g["debit"] - bal, 2)
            total_debit -= bal
        else:
            g["credit"] = round(g["credit"] + bal, 2)
            total_credit += bal

    rows = sorted(
        ({"group": name, **vals} for name, vals in groups.items()),
        key=lambda r: r["debit"] + r["credit"],
        reverse=True,
    )
    difference = round(total_debit - total_credit, 2)
    return {
        "ok": True,
        "total_debit": round(total_debit, 2),
        "total_credit": round(total_credit, 2),
        "difference": difference,
        "balanced": abs(difference) <= 1.0,
        "groups": rows,
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
