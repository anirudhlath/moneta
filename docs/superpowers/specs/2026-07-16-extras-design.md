# Extras (final wave) — design

**Date:** 2026-07-16
**Backlog tickets:** `low/notifications-digest.md`, `low/plaid-link-update-mode.md`,
`low/test-coverage-gaps.md`
**Wave:** 5 of 5. Branch `feature/extras` off main (post PR #11).
Owner-delegated decisions recorded inline.

## 1. Notifications digest (decided shape)

**Command:** `moneta digest` → `POST /digest` (a pipeline: it commits its cursor).
Cron recipe documented: `moneta sync && moneta digest`. No `sync --notify` flag
(composability over flags).

**Channel (v1):** ntfy.sh — config key `ntfy_topic` (`MONETA_NTFY_TOPIC`), the
full topic URL (e.g. `https://ntfy.sh/moneta-xyz123`). Secrets stay in
config/env. Unset → `/digest` returns 400 with a setup hint.

**What's sent:** one plain-text message (title "moneta digest") containing
(a) series events with `id >` the stored cursor — missed / price_increase /
new_series lines rendered like `recurring --events` text (`dollars()` is fine
here: prose), and (b) obligations whose `deferred_interest_risk` is true AND
whose account id is not already in the warned set. Nothing new → nothing sent
(no empty pings), cursor still advances past nothing.

**State (migration `0005`):** single-row table `digest_state`
(`id=1, last_event_id int NOT NULL DEFAULT 0, warned_account_ids JSON NOT NULL
DEFAULT []`). A risk that clears (payoff before promo) removes the id from the
warned set so a re-appearance re-notifies.

**Delivery:** `httpx.post(topic_url, content=body, headers={"Title": ...})`,
5s timeout. Failure → `logger.warning` + response `{"sent": false, "reason":
...}`; the cursor does NOT advance on delivery failure (events aren't lost).
`/digest` response: `{"sent": bool, "events": int, "warnings": int}`.
CLI prints a one-liner. `moneta digest --json` supported (read of the response;
the command IS a write — exempt it from `reject_json_with_writes` reasoning
since printing the POST result is the whole point; document).

## 2. Plaid Link update mode

`moneta setup plaid-relink <item-id>`: look up the stored item; create a hosted
link token WITH `access_token` set (update mode — new `plaid.create_hosted_link`
optional param), print URL, `poll_link_result`, and on completion keep the SAME
item entry (same `item_id`, same `access_token` — update mode re-authorizes in
place; no exchange call). Unknown item id → clean error listing `plaid-list`.
Real-API verification is impossible in tests → QA item filed by the wave.

## 3. Test-coverage gaps

Each ticket bullet becomes at least one test (no production changes expected;
split any real bug found): config env-precedence with file+env both set;
SimpleFIN errors-warning branch + non-2xx raise; events `expected_cents != 0`
zero-division guard; ingest unknown-account `continue` + fully-empty snapshot;
`cash_out` outflow-to-loan counted; `power_report` empty DB zeroes;
vesting duplicate-symbol across accounts + same-symbol-twice CSV (last wins —
pin whichever behavior exists); committed parameterized-date e2e anchors
(fixed `today` values: year boundary, leap day, month-end — as a parametrized
test over the date-relative scenario builder, NOT live today).

## Out of scope

Email channel, digest scheduling inside moneta (cron owns it), Plaid webhook
mode, the two review-born tickets (`llm-discretionary-not-sticky`,
`classification-first-class-type`) — they remain in backlog by design.

## Docs

user-guide: digest section (ntfy setup, cron recipe), plaid-relink; PRD
feature table/history + roadmap (digest + relink move out; "Later" section
prunes push notifications); README command table. Delete the three tickets.
