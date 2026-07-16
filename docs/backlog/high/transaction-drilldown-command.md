# Add a `moneta txns` transaction drill-down command

## Summary
There is no way to list raw transactions from the CLI or API. The most common
trust question — "why is spent-so-far $X?" — is unanswerable without opening
the SQLite file directly. Add a `GET /transactions` endpoint and a
`moneta txns` command with filters.

## Context / motivation
Every headline number (`power`'s spent-so-far, `cashflow`'s accrual/cash-out)
is an aggregate over transactions, with exclusions applied (transfer links,
series tagging, account-type filters). When a number looks wrong, the user
needs to see which transactions were counted and which were excluded, and why.
This also de-risks every other feature: recurring detection, transfer linking,
and normalization all become auditable.

## Acceptance criteria
- `GET /transactions` with query params: `start`, `end` (default: current
  month), `account_id`, `merchant` (substring match), and a flag to include
  exclusion metadata.
- Each row exposes: date, account, merchant, description, amount, and *why it
  is or isn't counted as spend* — series link (merchant of the series),
  transfer-link status (and classification: internal move vs loan payment),
  account type in/out of `SPEND_ACCOUNT_TYPES`.
- `moneta txns [--month YYYY-MM | --start D --end D] [--account ID]
  [--merchant NAME]` renders a rich table; excluded rows visibly marked
  (dim/label), not hidden.
- Read-only view: no commits (views don't commit).
- Money values are integer cents (`*_cents`), per the established API
  convention — no Decimal-as-string, no pre-formatted display strings.
