# Detection correctness — design

**Date:** 2026-07-16
**Backlog tickets:**
`docs/backlog/high/bills-vs-habitual-spending.md`,
`docs/backlog/high/synchrony-financing-account-typing.md`,
`docs/backlog/high/loan-payments-derive-from-links.md`,
`docs/backlog/medium/closest-cadence-needs-tolerance.md`,
`docs/backlog/medium/recurring-not-a-bill-cli-overrule.md`,
`docs/backlog/medium/ended-series-spend-visibility.md`,
`docs/backlog/low/merchant-normalization-improvements.md` (residual: `_STORE_NUM` only —
the direction-scoped review dedup bullet already shipped; verified in code)
**Wave:** 2 of 5 (backlog-clearing pass). Branch `feature/detection-correctness` off main
(after PR #8, the cents API, is merged — this wave's new fields are born on that convention).

## Problem

Four real-data failures, all verified on the owner's actual accounts (2026-07-15):

1. A weekly restaurant habit (amounts $21.76–$120.35, 5.5× spread) became a
   **fixed cost** (+$168.39/mo) because "is this a recurring bill?" is the wrong
   question — a habit *is* recurring — and the amount-instability signal was ignored.
2. Seven Synchrony store cards used as 0%-promo financing vehicles type as `credit`,
   so their very-real monthly payments vanish from `power` and `obligations` —
   the design doc's problem #4 failing on the author's own data.
3. Banks collapse payments to different loan accounts into one descriptor
   (`"Synchrony Bank Payment Web Id"` × 3 cards), so merchant-grouped detection
   forms one mixed-median series or nothing; `obligations` shows another card's
   number or `?`.
4. A gym's prorated first charge (gaps [12, 30]) made the forced-cadence fallback
   pick biweekly from the all-history median → $575.73/mo shown for a $265.72/mo gym.

Plus two smaller holes: ended-series transactions disappear from every power bucket,
and a wrongly-confirmed series can't be overruled from the CLI.

## Design decisions (settled with the owner)

- **Habits get a `discretionary` flag on `RecurringSeries`** — tracked (cadence,
  price events, visibility in `moneta recurring`) but never a fixed cost; their
  transactions count as discretionary spend.
- **Financing-mode cards get a dedicated `Account.financing_mode` flag** — the
  account remains `type=credit` (it *is* a credit card) but gains loan semantics
  in classification.
- **Per-loan-account payments are derived at view time** from transfer links —
  consistent with "obligations are derived, not stored". No synthetic series.

## Schema (migration `0003`, `_HEAD` → `"0003"`)

- `recurring_series.discretionary: BOOLEAN NOT NULL DEFAULT 0`
- `accounts.financing_mode: BOOLEAN NOT NULL DEFAULT 0`

One revision (`0003_discretionary_and_financing.py`), `down_revision="0002"`;
`tests/test_migrations.py` pins parity automatically.

## 1. Bills vs habitual spending

The classification power needs is three-way, and both LLM prompts plus the human
review flow adopt it:

- **bill** — fixed obligation: subscription, rent, insurance, loan/membership;
  roughly stable amount; consequences if unpaid.
- **habit** — recurring discretionary spending: restaurants, coffee, bars,
  groceries, rideshare; variable amounts; a choice each time.
- **not recurring** — coincidence, not a series.

Prompt changes (`_LLM_PROMPT` in `pipelines/recurring.py`, `_VERIFY_PROMPT` in
`pipelines/review.py`): define the bill/habit boundary explicitly and include the
amount spread — min / median / max over the judged run — in the prompt context.
Response schema becomes `{"classification": "bill"|"habit"|"not_recurring"}`
(verify keeps its `"confident"` field).

**Detection paths** (`detect_recurring`):

- Amount-**unstable** groups (failing the ±20% check) with a cadence match — the
  Uno Mas shape — require the stronger bar: LLM answers the three-way question.
  `bill` → normal series; `habit` → series with `discretionary=True`;
  `not_recurring`/malformed/no-LLM → review item (no series), as today.
- Cadence-miss groups: unchanged gate (stable + bill-like gaps → review item).
- The force map (`recurring_cluster` ledger) carries three states:
  suppressed (`is_recurring: false`), bill (`is_recurring: true`), habit
  (`is_recurring: true, discretionary: true`). Detection applies `discretionary`
  from the map on create *and* update, so a later human reclassification sticks.

**verify_series**: confident `bill` → resolved yes (feeds force map), as today.
Confident `habit`, confident `not_recurring`, unconfident, or malformed → open
item flagged `llm_flagged` (human-only), series stays active and non-discretionary
until the human rules — the LLM still never suppresses (or demotes) a
deterministic detection on its own. The open item's payload records the LLM's
lean (`llm_leaning: "habit"` etc.) so the human sees it in context.

**Human review flow** (`moneta review`, `_review_one`): recurring_cluster
questions accept `b` / `h` / `n` (bill / habit / not recurring; Enter skips).
Resolution payloads as in the force map above. `_REQUIRED_BOOL` validation still
demands `is_recurring: bool`; `discretionary` is optional and only meaningful
with `is_recurring: true`.

**apply_resolution** (recurring_cluster): `is_recurring: false` unchanged (ends
live series). `is_recurring: true` with `discretionary: true` sets
`discretionary=True` on the live series (and un-ends it if the *same* resolution
reopened a mistaken not-a-bill — see §5); with `discretionary: false`/absent it
clears the flag. Detection's force map keeps it consistent on later runs.

**Power view**: `income_sources` and `fixed_costs` filter
`discretionary == False` (see §6 for the spend side).

**Regression tests**: weekly merchant, >2× amount spread, scripted LLM `habit`
→ a discretionary series exists, `total_fixed_cents` contribution is 0, its txns
count in `spent_so_far_cents`; same shape with scripted `bill` → fixed cost as
today; stable weekly subscription with no LLM → detected exactly as today.

## 2. Financing-mode fingerprint

New pipeline stage: `pipelines/financing.py::detect_financing(session) -> int`
(count of questions opened), called by `run_sync` after transfers, before
auto-review. Deterministic; no LLM.

For each account with `type == credit`, `financing_mode == False`, and **no
existing `financing_account` ReviewItem** (open or resolved — the one-time
gate), evaluate the fingerprint over the account's own transactions:

- **Owed balance:** `balance_cents < 0`.
- **Payments:** ≥ 2 credit txns (`amount_cents > 0`), and ≥ 2 of them within
  ±20% (`_AMOUNT_TOLERANCE`) of the credit median — tolerates one payoff outlier
  (CareCredit's $2,847.94).
- **No active purchasing:** zero *significant* debits (abs ≥ 25% of the credit
  median — `_MINOR_FRACTION`, tolerating paper-statement fees) dated on or after
  the account's earliest payment credit. Tolerates Modani's financed purchase
  preceding its payments; OnePay's 39 interleaved purchases never fire.

Fingerprint match → open `ReviewItem(kind=financing_account,
question="'{account.name}' looks like promo financing being paid down — treat
its payments as fixed costs?", payload={"account_id": id})`. New
`ReviewKind.financing_account` member. Human-only: `autoreview_items` already
skips kinds it doesn't handle; add no LLM branch.

**Resolution** `{"financing": bool}` (new `_REQUIRED_BOOL` entry):
`apply_resolution` sets `account.financing_mode = resolution["financing"]`.
A `false` answer is remembered by the resolved ledger item — never re-asked.

**Loan-like classification** — `queries.ClassifiedLink` grows
`inflow_is_loan_like: bool` (`type == loan` OR `financing_mode`), and consumers
switch to it per the "extend classified_links, don't re-join" rule:

- `recurring._excluded_txn_ids`: loan-like replaces the `loan` check (see §3 —
  the branch flips meaning there).
- `views/cashflow.cash_out`: unchanged logic (excludes only liquid→liquid;
  credit was never liquid) — no edit needed, add a test pinning that a
  financing-mode payment counts as cash out.
- `views/power` cc-payment filter: `cc_series` becomes links into accounts that
  are credit **and not** financing-mode (financing payments are fixed costs, not
  double-count exclusions).
- `views/financing.compute_obligations`: includes loan-like accounts (financing
  cards get obligations rows and `--set-promo` deferred-interest warnings).

**Correctability:** `AccountPatch` gains `financing_mode: bool | None`;
CLI `moneta accounts --set-financing ID true|false`. `--set-type` overrides
remain untouched and continue to survive re-sync. `moneta accounts` shows
financing-mode accounts' type as `credit (financing)` in the table.

**Known limitation (document in the user guide, don't solve):** a hybrid card
carrying both daily spend and a promo plan can't be split from transaction data;
Plaid's liabilities product is the data-driven path if one ever appears.

**Regression tests:** each real shape above as a fixture — Musician's Friend
(3 × $64.44 exact, fires), CareCredit (payoff outlier, fires), Modani (purchase
then payments, fires on the 2nd payment), OnePay (39 purchases / 4 credits,
never fires), plus: resolved-false never re-asks; resolved-true flips
`financing_mode` and the account appears in obligations.

## 3. Loan payments derive from links

**Derivation** — new pure helper in `queries.py`:

```python
class LoanPayment(BaseModel):
    account_id: int
    cadence: Cadence          # _match_cadence over payment dates; fallback monthly
    expected_cents: int       # negative (outflow convention): -median(|amounts|)
    last_paid_on: date

def loan_payment_stats(links: list[ClassifiedLink]) -> dict[int, LoanPayment]
```

Groups links by `inflow_account_id` where `inflow_is_loan_like`; dates from
`outflow_posted_on`, amounts from the new `ClassifiedLink.outflow_amount_cents`
field. Cadence via `_match_cadence` (imported from `pipelines.recurring`, same
precedent as `monthly_cents`); **fallback monthly** when no run matches — loans
are near-universally monthly, and this covers 2-payment accounts. Expected =
median of the matched run's amounts (all amounts when fallback).

**Consumers:**

- `views/power`: after the series-based fixed costs, append one line per
  loan-like account with a derived payment: merchant label
  `f"{account.name} — payment"`, monthlyized via the existing `_PER_MONTH`
  factors. Sorted into the same list.
- `views/financing.compute_obligations`: `monthly_payment_cents` from the
  derivation (`abs`, monthlyized); `_payment_series_id` and its series lookup
  are deleted. `months_left`/payoff math unchanged.

**Detection exclusion** (`recurring._excluded_txn_ids`): outflows linked to
loan-like accounts are now excluded from merchant grouping **entirely** (the
old rule kept them in; the view-time derivation replaces the merchant series).
Migration of existing data, same run: at the top of `detect_recurring`, untag
(`series_id = None`) any txn in the loan-linked excluded set; an active series
left with zero tagged txns after this is ended in the sweep (extend the sweep:
series whose txns were untagged here and have no remaining tagged txns → ended).
No double-counting window — power's per-account lines take over on the first
sync after upgrade.

**Regression tests:** two loan accounts paid from checking with identical
descriptors but different amounts/dates → two distinct obligations with correct
per-account amounts and two power fixed-cost lines; a pre-existing merged
merchant series over those payments is ended (not summed) on the next
`detect_recurring` run; a 2-payment loan account gets a monthly-fallback
payment, not `?`.

## 4. Cadence fallback tolerance

`_closest_cadence(dates)` (used only on the forced path) is replaced:

- Candidate = cadence nearest to the **newest gap** (last inter-date gap).
- Accept only if `|newest_gap − CADENCE_DAYS[c]| <= _TOLERANCE[c]`.
- Otherwise, and when there are < 2 unique dates (no gaps — today this raises
  `StatisticsError`), fall back to **monthly** with a code comment documenting
  why (safest cadence: factor 1.0 can't inflate the monthly number).

**Regression tests:** charges at day 0/+12/+42 with a forced-recurring answer →
**monthly** series (the Lifetime Fitness shape); a single-date forced group →
monthly, no crash; groups where `_match_cadence` finds a run → unchanged.

## 5. Not-a-bill overrule (CLI + API)

- `POST /recurring/{series_id}/not-a-bill`: 404 for unknown id; otherwise find
  the `recurring_cluster` ledger item by `series_key(series.merchant,
  series.direction)` — reopen it if resolved, create it if absent — then
  `apply_resolution(item, {"is_recurring": False}, resolved_by="manual")`
  (ends the live series immediately; force map suppresses forever).
- `POST /recurring/{series_id}/habit`: same find-or-create, resolution
  `{"is_recurring": True, "discretionary": True}` — the Uno Mas remedy that
  keeps tracking. Sets the flag on the live series via apply_resolution (§1).
- `POST /recurring/{series_id}/re-review`: reopens the ledger item
  (`status=open`, `resolution=None`) so the next `moneta review` re-asks the
  human; series state untouched. Covers the inverse mistake.
- CLI: `moneta recurring --not-a-bill ID | --habit ID | --re-review ID`
  (mutually exclusive with each other and with `--end`; combining errors
  cleanly). Help text updated. `cli/` stays zero-logic — each flag is one POST.

**Regression tests:** series with a forced-True LLM ledger entry +
`--not-a-bill` → series ended, ledger flipped manual/false, next
`detect_recurring` does not recreate it (the ticket's acceptance case);
`--habit` → series active + discretionary, excluded from fixed costs;
`--re-review` → item open again; unknown ID → clean 404 CLI error, no traceback.

## 6. Ended-series spend visibility

Power's `spent_so_far` exclusion narrows: a transaction is excluded from
discretionary spend only when tagged to an **active, non-discretionary** series
(those are represented in fixed costs). Tagged-to-ended and tagged-to-
discretionary txns count as spend. Implementation: outer-join `RecurringSeries`
in the month-txns query and include txns where `series_id IS NULL OR status !=
'active' OR discretionary`. Covers both this ticket and the discretionary
bucket from §1 with one rule.

**Regression test (ticket's acceptance):** end a series, sync a new matching
transaction, assert it lands in `spent_so_far_cents` and
`remaining_cents` reflects it.

## 7. `_STORE_NUM` residual

`normalize.py`: `_STORE_NUM = re.compile(r"(#\d+|\b\d{3,}\b)")` →
digit runs hyphen-joined to word characters are kept:
`re.compile(r"(#\d+|(?<![\w-])\d{3,}(?![\w-]))")`.
`1-800-FLOWERS` keeps its 800; `BLUE BOTTLE #1234` and `STORE 4521` still strip.
Regression tests for all three shapes.

## Pipeline order (run_sync)

ingest → normalize → transfers → **financing-detect (new)** → auto-review →
recurring → verify → events. Financing detection reads transfers' account data
only; placing it before auto-review keeps all question-opening stages adjacent.
`SyncReport` gains a `financing_questions: int` field (CLI mentions it in the
review nudge line only when non-zero).

## Out of scope

- Per-field sign semantics (`docs/backlog/medium/api-money-sign-semantics.md`).
- Hybrid financing cards (documented limitation).
- `--reactivate` CLI (wave 4), txns drill-down (wave 3).
- Missed-payment events for view-derived loan payments (series events don't
  cover them since no series exists; acceptable — obligations shows the payment
  and balance; ticket-worthy later if wanted).

## Docs

- `docs/PRD.md`: feature-history entries; move the shipped backlog items out.
- `docs/user-guide.md`: three-way review answers, `--not-a-bill`/`--habit`/
  `--re-review`, `--set-financing`, financing fingerprint + hybrid limitation,
  obligations derivation note.
- `CLAUDE.md`: update the transfer-link semantics bullet (loan-like, per-account
  derivation), series lifecycle (discretionary), pipeline order (financing
  stage), LLM ledger (three-way).
- Delete the seven shipped ticket files (merchant-normalization's remaining
  bullet ships here, so the whole file goes).
