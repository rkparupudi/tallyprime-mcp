# tallyprime-mcp

A read-only MCP server that lets Claude answer questions from the books in a
locally running TallyPrime: ledgers, vouchers, outstanding bills and the trial
balance.

It runs on the same Windows machine as TallyPrime and talks to Tally's local
HTTP server. The books never leave the machine except as the answers Claude
reads in the conversation.

## Tools

| Tool | What it answers |
|---|---|
| `list_companies` | Which companies are loaded; doubles as a connection check |
| `list_ledgers` | Ledger balances, optionally by group or name |
| `get_vouchers` | Transactions in a date range, with totals by voucher type |
| `get_outstanding` | Receivables or payables with ageing, per bill and per party |
| `get_trial_balance` | Debit and credit totals by group, and whether they balance |

Every request sent to Tally is an `Export`. The client refuses to send
anything else, so this version cannot create or change data.

## Setup

### 1. Turn on Tally's HTTP server

In TallyPrime: **F1 (Help) > Settings > Advanced Configuration**, enable the
HTTP server and keep port **9000**. Restart TallyPrime and load a company.

Check it from a browser: `http://localhost:9000` should answer with a short
"TallyPrime Server is Running" message.

### 2. Install

Needs Python 3.10 or newer.

```
cd tallyprime-mcp
pip install -e .
```

### 3. Add it to Claude Desktop

Settings > Developer > Edit Config, then add:

```json
{
  "mcpServers": {
    "tallyprime": {
      "command": "tallyprime-mcp",
      "env": {
        "TALLY_URL": "http://localhost:9000",
        "TALLY_COMPANY": ""
      }
    }
  }
}
```

Restart Claude Desktop. If `tallyprime-mcp` is not found, use
`"command": "python", "args": ["-m", "tallyprime_mcp.server"]` instead.

### Settings

| Variable | Default | Meaning |
|---|---|---|
| `TALLY_URL` | `http://localhost:9000` | Where Tally's HTTP server listens |
| `TALLY_COMPANY` | empty | Company to query; empty means the active company |
| `TALLY_TIMEOUT` | `60` | Seconds to wait for Tally |

Keep `TALLY_URL` pointed at a machine you trust. Tally's HTTP server has no
authentication, so do not expose port 9000 beyond the local machine.

## Try it without Tally

A mock Tally with sample data is included:

```
python tests/mock_tally.py 9000
```

Then point the server at it and ask things like:

- "Which customers have bills older than 60 days?"
- "Summarise October's sales and purchases."
- "Does the trial balance tally?"

## Tests

```
pip install -e ".[dev]"
pytest
```

## Conventions

- Balances are returned as an amount plus a side (`debit` / `credit`).
  Internally Tally stores debits as negative numbers.
- `get_outstanding` ages bills from the bill date, and reports
  `days_overdue` from the due date when Tally provides one. It only covers
  ledgers that maintain bill-wise details.
- `get_trial_balance` groups by each ledger's immediate parent group, not by
  Tally's primary groups.

## Not yet done

- Voucher creation (needs a confirm-before-write step).
- Stock and inventory, GST reports, profit and loss, balance sheet.
- Packaging as a one-click desktop extension.
