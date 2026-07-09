# LLM Verification of Deterministic Recurring Series and Amount Changes

**Date:** 2026-07-09
**Status:** Approved (autonomous session — design decisions resolved from codebase patterns; PR is the user review gate)
**Author:** Anirudh Lath (design session with Claude)

## 1. Problem

Two paths shape fixed costs with no LLM second opinion today:

1. **Clean deterministic detections never see the LLM.** `detect_recurring` consults the
   LLM only when cadence matches but amounts are unstable. A group with clean cadence
   *and* stable amounts (weekly grocery runs, habitual same-store fill-ups) becomes a
   `RecurringSeries` silently and lands in fixed costs, understating spending power —
   or worse for inflows, a regular non-income deposit counts as income, overstating it.
2. **Amount updates follow a single transaction unreviewed.** `emit_series_events`
   rewrites `expected_cents` from the *single latest* tagged transaction whenever it
   drifts >5% from expected. One anomalous charge at a bill merchant (an annual true-up,
   a one-off purchase at the same merchant) silently becomes the series' new expected
   amount and flows straight into `moneta power`.

Goal: when an LLM is configured, every deterministically detected series and every
amount change gets an LLM review; anything the LLM can't confidently wave through goes
to the human review queue. With no LLM configured, behavior is unchanged — deterministic
detection remains the trusted default (design §9: the LLM is a classifier gating
decisions, never a dependency and never a source of money values).

## 2. Approaches considered

- **A. Inline gates inside `detect_recurring` at series creation.** Consistent with the
  existing unstable-amounts inline LLM call, but only covers series created after the
  feature ships — pre-existing series in the user's DB are never reviewed — and tangles
  detection with verification bookkeeping.
- **B. Post-detection verification step in the same sync run (chosen).** Detection stays
  purely deterministic; a new `verify_series` step reviews every active series that lacks
  a verification record, covering new and pre-existing series with one mechanism and no
  schema change. Amount changes are gated where they happen, in `emit_series_events`.
- **C. Queue everything as ReviewItems and let `autoreview_items` resolve them next
  sync.** Maximum reuse, but adds a full sync-cycle delay to every verification and is
  circular: the auto-reviewer would re-ask the same LLM the question it just failed to
  answer confidently.

## 3. Design

### 3.1 Series verification — `verify_series` (pipelines/review.py)

New step in `run_sync`, between `detect_recurring` and `emit_series_events`:

ingest → normalize → transfers → auto-review → recurring → **verify** → events

- **Target set:** active series whose merchant has *no* `recurring_cluster` ReviewItem,
  open or resolved. The ReviewItem table doubles as the verification ledger — no schema
  change, an audit trail visible in `moneta review`, and merchants already resolved by a
  human (or previously verified) naturally skip. Matching is by merchant only, consistent
  with the existing force-map semantics in `detect_recurring`.
- **Prompt:** merchant, direction, cadence, expected amount (dollars, formatted at the
  boundary), and recent tagged occurrences (date, amount). Response:
  `{"is_recurring": true/false, "confident": true/false}`.
- **Confident yes** → write a *resolved* ReviewItem
  (`resolution={"is_recurring": true, "resolved_by": "llm"}`). This is both the
  verified-marker and, via the existing force map, tells future detection runs the
  merchant is settled.
- **Anything else** (confident no, unconfident, LLM error/malformed) → *open* a
  ReviewItem with `payload={"merchant", "direction", "llm_flagged": true}`. The series
  stays active meanwhile: a disputed fixed cost keeps counting until the human rules,
  so the failure direction is understated spending power — the safe one. The LLM never
  suppresses a deterministic detection; it either waves it through silently or escalates
  to the human.
- **`llm is None`** → no-op. No queue flooding; today's behavior exactly.
- Returns counts (verified, flagged) surfaced in `SyncReport` and the CLI sync line.

`autoreview_items` skips items whose payload has `llm_flagged: true` — they exist
because the LLM already looked and couldn't (or shouldn't) settle them; re-asking the
same model at temperature 0 is circular. These items are human-only.

### 3.2 Amount-change gate (pipelines/events.py)

`emit_series_events` gains the `llm: Classifier | None` parameter (bringing the last
pipeline in line with the "every pipeline takes `llm`" convention). On >5% drift between
the latest tagged transaction and `expected_cents`:

- **`llm is None`** → apply as today (event + `expected_cents = latest.amount_cents`).
- **LLM confident `is_price_change`** → apply as today. The money value still comes from
  the transaction; the LLM only gates.
- **Anything else** → do *not* update. Open a ReviewItem
  (`kind=price_change`, `payload={"series_id", "merchant", "old_cents", "new_cents",
  "occurred_on", "llm_flagged": true}`).
- **Re-ask suppression:** skip drift handling for a series when an open `price_change`
  item exists for it, or a resolved one for the same `(series_id, new_cents)` answered
  `is_price_change: false`. A later drift to a *different* amount asks a fresh question.

### 3.3 New ReviewKind: `price_change`

End-to-end support mirroring the existing kinds:

- `models.ReviewKind.price_change`.
- `review_context`: old/new amounts and recent tagged occurrences for display.
- `_validated`: accepts `{"is_price_change": bool}` (confident only, per existing rule).
- `apply_resolution`: `is_price_change: true` → set `series.expected_cents =
  payload["new_cents"]` and emit the `price_increase` SeriesEvent (same effect the
  deterministic path would have had); `false` → resolve only (the resolved item is the
  suppression record).
- CLI `_REVIEW_KINDS` entry + `_review_one` branch: show "old → new" amounts, y/n prompt.
- `autoreview_items` needs no `price_change` prompt branch: every `price_change` item is
  opened `llm_flagged`, and flagged items are skipped by the uniform rule in §3.1. The
  kind is human-only by construction.

### 3.4 Fix: resolving "not recurring" ends the live series

Today `apply_resolution` for `recurring_cluster` only records the answer; the force map
suppresses *future* detection, but an already-created active series keeps counting in
fixed costs until it goes stale (~3 cadence periods). With verification now opening
items on live series, this gap becomes user-visible. Fix: when a `recurring_cluster`
item resolves with `is_recurring: false` and an active series exists for the payload's
(merchant, direction), set its status to `ended` immediately. Applies to human and LLM
resolutions alike.

## 4. LLM boundary (design §9 compliance)

- All new LLM outputs are booleans (`is_recurring`, `is_price_change`, `confident`).
- No LLM output ever supplies a money value: verified amounts come from transaction
  medians (detection) or the transaction itself (price change); the LLM only decides
  whether a deterministic value is applied or queued.
- Every LLM decision lands in a ReviewItem (`resolved_by: "llm"`) — auditable and
  correctable via `moneta review`.
- No LLM configured → identical behavior to today, except human review items opened by
  this feature never appear (verification is a safety net, not a dependency).

## 5. Cost profile

One LLM call per unverified active series — the first sync after shipping backfills the
user's existing series (typically tens of calls, comparable to first-sync merchant
normalization); subsequent syncs only pay for genuinely new series. One call per drift
event, suppressed thereafter by the open/resolved item. All calls short-circuit on
`llm is None`.

## 6. Testing

Fake `Classifier` stubs per existing test style (`tests/test_recurring.py`,
`test_autoreview.py`). Cases:

1. Deterministic new series, LLM confident-yes → series active, resolved ReviewItem
   recorded, nothing re-asked on the next run.
2. Confident-no / unconfident / LLM error → series active **and** open `llm_flagged`
   item; `autoreview_items` leaves it untouched.
3. Human resolves that item `is_recurring: false` → series ended immediately, group
   suppressed on future runs (force map).
4. `llm=None` → no items, behavior byte-identical to today.
5. Price drift, confident-yes → `expected_cents` updated + `price_increase` event
   (today's behavior preserved).
6. Price drift, unconfident → no update, open `price_change` item; same drift on the
   next run does not duplicate the item or re-call the LLM.
7. Human (or API) resolves `is_price_change: true` → amount applied + event emitted;
   `false` → no update ever for that (series, amount) pair.
8. Price drift, `llm=None` → auto-applied as today.
9. `run_sync` report carries the new verified/flagged counts; CLI prints them.

## 7. Out of scope

- Re-verifying a series after it's been verified once (the ledger is permanent unless
  the user deletes/re-answers the review item).
- LLM review of series *revival* (ended → active) — the merchant was already verified.
- Amount estimation or correction by the LLM (forbidden by §9).
- Migration tooling — the design deliberately adds no schema columns; the only model
  change is a new enum member on a `String` column.
