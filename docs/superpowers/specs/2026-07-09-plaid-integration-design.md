# Plaid Integration — Design

**Date:** 2026-07-09
**Status:** Approved for implementation
**Parent spec:** `2026-07-07-moneta-design.md` (§4 anticipated "Plaid can be added without touching pipelines"; §11 lists "Plaid adapter where SimpleFIN coverage is weak")

## 1. Goal

Add Plaid as a second aggregator so institutions with weak or missing SimpleFIN
coverage can be synced directly. Plaid and SimpleFIN must coexist: one `moneta sync`
pulls from every configured source through the existing `AggregatorAdapter` protocol,
and no pipeline, view, or model changes its behavior based on which aggregator
supplied a row.

Non-goals (v1): webhooks, Link update mode (re-auth repair), incremental cursor
persistence, Plaid liabilities/investments-transactions products, multi-user.

## 2. Verified API facts the design rests on

Grounded against official Plaid docs (July 2026):

- Environments: only `sandbox` and `production` (`https://{env}.plaid.com`);
  Development was retired June 2024. Personal use fits the free Trial /
  pay-as-you-go plans; sandbox is free.
- **Hosted Link is GA and webhook-free.** `POST /link/token/create` with a
  `hosted_link: {}` block returns a `hosted_link_url` the user opens in any
  browser. The CLI then polls `POST /link/token/get`; on completion the public
  token appears at `link_sessions[].results.item_add_results[].public_token`
  (with `institution` metadata beside it). Exchange via
  `POST /item/public_token/exchange` → `access_token` + `item_id`.
- `POST /transactions/sync` is the canonical transactions endpoint
  (`/transactions/get` is legacy). Cursor-paginated, `count` ≤ 500. First call
  with empty cursor replays all history (up to `transactions.days_requested`,
  max 730, set at link-token creation). Response carries
  `transactions_update_status` (`NOT_READY` → historical pull still running) and
  a `TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION` error means "restart the
  pagination loop from the first-page cursor".
- **Amount sign is inverted vs moneta:** Plaid reports positive when money
  leaves the account. Moneta/SimpleFIN use negative = outflow. The adapter
  negates every amount.
- `POST /accounts/get` returns accounts with `type`/`subtype` and an `item`
  object that now includes `institution_name` — no `/institutions/get_by_id`
  call needed. Liability accounts (credit, loan) report `balances.current` as a
  positive amount owed; moneta stores owed balances negative (SimpleFIN
  convention), so the adapter negates them.
- `POST /investments/holdings/get` returns `holdings[]` (quantity,
  institution_value) plus a `securities[]` lookup for ticker symbols. Items
  without the product raise `PRODUCTS_NOT_SUPPORTED` or `NO_INVESTMENT_ACCOUNTS`.
- Errors are JSON `{error_type, error_code, error_message, ...}`;
  `ITEM_LOGIN_REQUIRED` means the user must re-link.
- `plaid-python` is still synchronous-only.

## 3. Approaches considered

**HTTP client.** (a) official `plaid-python` SDK — sync-only, so every call
would need `asyncio.to_thread`, plus a heavyweight generated dependency for the
six endpoints we use; (b) **raw `httpx` (chosen)** — matches the SimpleFIN
adapter exactly, async-first, full control under `mypy --strict`.

**Link flow.** (a) **Hosted Link + CLI polling (chosen)** — zero
infrastructure: print URL, user completes in browser, CLI polls
`/link/token/get`; (b) serve a Plaid Link page from the FastAPI app — requires
`moneta serve` running, a browser on the same host, and JS; (c) manual
public-token paste — worst UX. Hosted Link mirrors the `setup simplefin`
one-command experience.

**Sync strategy.** (a) **stateless full replay (chosen)** — every fetch runs
`/transactions/sync` from an empty cursor; ingest's `(account_id,
aggregator_id)` dedup absorbs the overlap, and history is bounded at 730 days,
so worst case is a handful of 500-row pages per item; (b) persisted cursors —
faster, but a cursor written at fetch time and an ingest that later fails
silently loses that batch (cursor must only advance after commit, which the
`fetch() → Snapshot` protocol can't express), and `--full` would need an
epoch-sentinel reset. Cursor persistence is a backlog optimization if sync ever
feels slow.

**Coexistence.** A `MergedAdapter` composite fans `fetch()` out to every
configured adapter concurrently and concatenates snapshots, so `run_sync` still
sees exactly one `AggregatorAdapter`. Plaid account IDs are per-item random
strings; no namespace collision with SimpleFIN IDs is realistic, and
`Account.aggregator_id` stays unique.

## 4. Components

### 4.1 `aggregator/plaid.py` (new)

- `PlaidError(Exception)` — carries `error_type`, `error_code`, `message`.
- `PlaidClient` — thin async JSON-POST wrapper: base URL from env
  (`sandbox`/`production`), injects `client_id`/`secret` into each body,
  raises `PlaidError` on error responses, accepts an injected
  `httpx.AsyncClient` for tests (SimpleFIN pattern).
- `PlaidItem` (pydantic) — `item_id`, `access_token`, `institution_name`,
  `products: list[str]`.
- Item store — `load_items(path)` / `save_items(path, items)` on
  `<config_dir>/plaid_items.json`.
- Link helpers (used by the CLI, mirroring `claim_setup_token`):
  - `create_hosted_link(client, products, days_requested=730)` →
    `(link_token, hosted_link_url)`; `client_name="moneta"`,
    `user.client_user_id="moneta"`, `country_codes=["US"]`, `language="en"`.
  - `poll_link_result(client, link_token, timeout, interval)` → polls
    `/link/token/get` until `item_add_results` is non-empty; returns
    `(public_token, institution_name)`.
  - `exchange_public_token(client, public_token)` → `(access_token, item_id)`.
- `PlaidAdapter` — implements `AggregatorAdapter`. `fetch(since)` **ignores
  `since`** (full replay per §3) and, per item:
  1. `/accounts/get` → `AccountDTO`s. `type_hint` from the type/subtype map
     below; balance = `balances.current` (fallback `available`, else 0),
     negated for `credit`/`loan` types; `balance_date` from
     `balances.last_updated_datetime` (fallback: today). Money parsed via
     `Decimal(str(x))` — Plaid sends JSON floats.
  2. If `"transactions"` in item products: `/transactions/sync` loop from empty
     cursor until `has_more` is false; skip `pending` rows; negate amounts;
     `description` = `name`; `raw` = full transaction object. On
     `TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION`, restart the loop (bounded
     retries). If `transactions_update_status == "NOT_READY"`, log that
     history is still preparing — the next sync picks it up.
  3. If `"investments"` in item products: `/investments/holdings/get` →
     `HoldingDTO`s; symbol from `securities[].ticker_symbol` (fallback:
     security name, then `"?"`); value from `institution_value`.
  - An item that fails with `ITEM_LOGIN_REQUIRED` is skipped with a
    `logger.warning` naming the institution and the fix
    (`moneta setup plaid-link` to re-link); other items and adapters still sync.

Account type mapping (`type_hint`): depository/checking → `checking`;
depository/anything else (savings, cd, money market, hsa, cash management,
prepaid) → `savings`; credit → `credit`; loan → `loan`; investment →
`brokerage`; other/unknown → `None` (falls back to existing name inference).

### 4.2 `aggregator/base.py`

- `AccountDTO` gains `type_hint: AccountType | None = None` (import from
  `models` — no circularity; `models` imports nothing from `aggregator`).
  SimpleFIN leaves it unset.
- `MergedAdapter` — holds a list of adapters; `fetch()` runs them with
  `asyncio.gather` and concatenates the three DTO lists.

### 4.3 `pipelines/ingest.py`

New accounts use `dto.type_hint or infer_account_type(name, org_name)`.
Existing accounts keep their stored type (user overrides survive re-sync,
unchanged).

### 4.4 `config.py`

`Settings` gains `plaid_client_id: str | None`, `plaid_secret: str | None`,
`plaid_env: str = "production"`. All flow through the existing TOML +
`MONETA_*` env override machinery (`MONETA_PLAID_CLIENT_ID`, …).

### 4.5 `api.py`

`build_app` assembles the adapter: SimpleFIN if `simplefin_access_url`, Plaid
if credentials **and** at least one stored item; one adapter → used directly,
two → `MergedAdapter`, none → `None`. The `/sync` 400 message becomes
"No aggregator configured. Run: moneta setup simplefin <token> or
moneta setup plaid <client_id> <secret>".

### 4.6 CLI (`cli/main.py`)

Flat commands under the existing `setup` group (SimpleFIN precedent: setup is
local config management, done CLI-side):

- `moneta setup plaid CLIENT_ID SECRET [--env production|sandbox]` — saves
  credentials to config; prints next step.
- `moneta setup plaid-link [--product transactions --product investments]` —
  creates a hosted link, prints the URL, polls until completion (15-min
  timeout, 3-s interval), exchanges the token, appends to
  `plaid_items.json`, prints "Linked <institution>. Run: moneta sync" (plain
  sync suffices — the Plaid adapter always replays full history). Default
  products: `transactions`.
- `moneta setup plaid-list` — table of linked items (institution, item_id,
  products).
- `moneta setup plaid-unlink ITEM_ID` — calls `/item/remove` (stops Plaid
  billing), drops the item from the store. Accounts/transactions already
  synced stay in the DB.

### 4.7 Docs

README quickstart + configuration sections gain the Plaid path; project
CLAUDE.md gains the two Plaid gotchas (sign inversion, stateless full replay).

## 5. Data flow

```
moneta setup plaid <id> <secret>          config.toml: plaid_client_id/secret/env
moneta setup plaid-link                   hosted URL → browser → poll → exchange
                                          → plaid_items.json (+access_token)
moneta sync
  build_app → MergedAdapter[SimpleFIN?, Plaid?]
  run_sync → adapter.fetch(since)         Plaid ignores since; full replay
  → Snapshot(accounts+txns+holdings)      signs normalized to moneta convention
  → ingest (dedup on aggregator ids)      type_hint beats keyword inference
  → normalize → transfers → review → recurring → events   (unchanged)
```

## 6. Error handling

- `PlaidClient` maps non-2xx / error bodies to `PlaidError` with code + message.
- Item-level auth failures (`ITEM_LOGIN_REQUIRED`) degrade to a warning + skip,
  keeping the rest of the sync alive (matches SimpleFIN's log-and-continue
  handling of per-institution errors).
- Credential-level failures (`INVALID_API_KEYS` etc.) propagate — the sync
  should fail loudly when nothing can be fetched.
- Link polling times out with a clear message; re-running `plaid-link` creates
  a fresh link token.
- Mutation-during-pagination restarts the sync loop, max 3 attempts, then
  raises.

## 7. Testing

Mirrors `test_simplefin.py`: `httpx.MockTransport` handlers scripted per
endpoint, injected client, no network.

- Adapter: snapshot parsing (accounts/txns/holdings), **sign negation both for
  amounts and liability balances**, pending skip, account-type mapping,
  multi-page `/transactions/sync`, mutation-restart retry, `ITEM_LOGIN_REQUIRED`
  skip-and-continue, products gating (no holdings call without `investments`).
- Link flow: hosted-link creation payload (`hosted_link` block,
  `days_requested`), poll-until-complete, exchange.
- Item store: round-trip, missing file → empty list.
- `MergedAdapter`: concatenation across two fake adapters.
- Ingest: `type_hint` wins over inference; absent hint falls back.
- Wiring: `build_app`/settings combinations (none / SimpleFIN only / Plaid only
  / both) select the right adapter shape.
- CLI: `setup plaid` writes config; `plaid-link` happy path with mocked client;
  `plaid-list`/`plaid-unlink` against a temp items file.

## 8. Risks / limitations (accepted for v1)

- **Re-linking creates new accounts.** Plaid account IDs are per-item; if an
  item is unlinked and re-linked, synced accounts duplicate. Manual cleanup for
  now (same exposure SimpleFIN has when a bridge connection is rebuilt).
- **No update mode.** `ITEM_LOGIN_REQUIRED` requires unlink + fresh link.
- **First sync may be partial** while Plaid's historical pull runs
  (`NOT_READY`); the stateless replay means the next sync completes it.
- **Access tokens on disk** in `plaid_items.json` (0600 via write) — same trust
  model as the SimpleFIN access URL already stored in `config.toml`.
- Backlog: cursor-based incremental sync; surfacing per-item sync warnings in
  the CLI report; Link update mode.
