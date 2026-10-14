# Automatic merchant merging and per-bill series

Status: approved by the user on 2026-10-13; refined while planning (merge rule, split rule, decision amounts).

## Intended outcome

One real bill must form one recurring series, even when the bank describes it
differently on different cards or providers. The user should not have to merge
anything by hand. Two different bills from one merchant (a subscription and
irregular top-ups, or two subscriptions at the same price) must stay separate,
and charges that follow no schedule must never count as fixed costs.

Success, checked on a rehearsal copy of real data:

- descriptors of one bill that differ only by wallet prefix, legal suffix,
  location or street address merge into one merchant and one series;
- a merchant's subscription and its irregular purchases stay apart; only the
  subscription is a series;
- two concurrent subscriptions at one price from one merchant are two series;
- two income streams from one payer at clearly different amounts stay two series;
- re-running sync changes nothing (idempotent).

This is architectural: it changes merchant identity and the identity of a
recurring series, which review decisions, verification and reconciliation
key on.

## Before implementation: behavior and evidence

`normalize.rule_normalize` strips a few processor prefixes (`sq *`, `tst*`,
`paypal *`, `py *`, `amzn mktp us*`), reference tokens and store numbers, then
title-cases. It keeps wallet prefixes (`AplPay`, `DD *`), legal suffixes
(`LLC`), and city, state, street and `USA` tails. One bill paid from two cards
therefore yields names like `Acme Fitness Llc Riverton Tx` and `Acme Fitness`.

`renormalize_merchants` re-applies rules only when `/normalize/rerun` is called,
and it skips renaming a series whose new name collides with an existing one.

`detect_recurring` groups transactions by `(merchant, direction)`, and
`RecurringSeries` is unique on that pair. A group drops charges below 25% of its
median, matches a cadence on the newest run, and requires amounts within 20% of
the median or asks the LLM. Review decisions (`recurring_cluster` items) and
their consumers (`detect_recurring`'s force map, `verify_series`,
`reconcile`'s supersede rules, the `/recurring/{id}` ledger endpoints) all key
on `(merchant, direction)`.

On the owner's data, grouping series by Plaid's clean merchant name found several
merchants split across two or three series each. It also found three cases where one
merchant legitimately has several streams: a subscription plus orders, two
same-price subscriptions billing on different days, and two income streams
from one payer. So merging names alone would break detection. Exact figures stay
in the owner's private notes, not in this repository.

## Approach

Two separate questions, answered deterministically:

1. **Which merchant?** Stronger rules, plus a merge pass that runs every sync.
2. **Which bill?** Split a merchant's charges into amount bands and concurrent
   tracks, but only when a schedule has extra charges alongside it.

Alternatives considered:

- *Plaid's `merchant_name` as identity*: accurate where present, but it covers
  only Plaid rows, gives no SimpleFIN-only signal, and merges distinct streams
  from one payer.
- *LLM matching of similar names*: handles bank truncation, but it is
  nondeterministic and adds per-sync calls. It is deferred to a later slice for
  whatever this one leaves split.

## Design

### 1. Merchant canonicalization

`rule_normalize` additionally strips, case-insensitively:

- wallet and processor prefixes, repeatedly (`AplPay TST* Name` → `Name`):
  `aplpay`, `apple pay`, `gpay`, `google pay`, `dd *`, `sp *`, plus the existing
  `sq *`, `tst*`, `paypal *`, `py *`, `amzn mktp us*`;
- a street address: a number (optionally ordinal, `3rd`) followed by up to three
  words and a street type (`street`, `st`, `road`, `rd`, `avenue`, `ave`, `blvd`,
  `drive`, `dr`, `lane`, `ln`, `way`, `pkwy`), together with everything after it.
  This runs before store-number stripping, which would otherwise eat the number;
- the legal tokens `llc`, `inc`, `corp`, `ltd` anywhere, and `co` when trailing;
- `*`, which becomes a space;
- trailing `usa` / `us`, then one trailing US state or territory code. At least one
  token always remains.

The city left behind (`Acme Fitness Riverton`) is handled by the merge pass.

**Merge pass** (`normalize.merge_targets`, new, pure) runs every sync inside
`renormalize_merchants`, right after `normalize_merchants`. It compares merchant names as token lists, lower-cased,
with leading digits stripped from each token (`0Elmton` → `elmton`). A name `B`
merges into a shorter name `A` when `A`'s tokens are a leading prefix of `B`'s,
`A` has at least 4 letters, and either:

- `A` has two or more tokens (`Acme Fitness` absorbs `Acme Fitness Riverton`), or
- `A` is a single token and `B` adds exactly one token that is a **place**. A
  place is a token that appears as the one extra token after three or more
  distinct existing names (`Elmton`, `Riverton`). It is learned from the data each
  run, not from a bundled list.

`B` merges into the shortest qualifying `A`. Merging rewrites non-manual
aliases and their transactions. Manual aliases are never rewritten, but they
can be merge targets.

The single-token restriction protects brand + product names: `Streamco` must
not absorb `Streamco Plus`, `Streamco Energy` or `Streamco Insurance`. A
read-only trial against the owner's descriptors confirmed both behaviours: the
known split bills merged, and the multi-product brands stayed apart.

`renormalize_merchants` now runs every sync. It re-applies the rules first, so
improved rules reach existing rule and LLM aliases, then merges the results. It still rewrites an LLM alias only
when the rule output looks clean. Both passes are idempotent. Every rename
(`old → new`) also renames matching `RecurringSeries.merchant` values and
`recurring_cluster` payload merchants. Uniqueness is gone (§3), so a rename never
has to be skipped.

### 2. Splitting a merchant into bills

`pipelines/bills.py` (new, pure functions) turns one `(merchant, direction)`
group into candidate bills:

1. **Amount bands.** Sort charges by absolute amount. A charge joins the
   current band when it is at most 20% above the previous charge; otherwise it
   starts a new band.
2. **Concurrent tracks.** Some bands match no weekly or biweekly cadence. For
   those, try monthly and then annual tracks. Walk the band's charges in date
   order, and attach each one to the track whose next expected date (one period
   after its last charge) is nearest, within that cadence's tolerance; otherwise
   start a new track. The band splits into tracks only when every charge lands
   on a track of three or more dates and two of those tracks overlap in time.
   Two same-price subscriptions billing on different days become two units. A
   subscription whose billing day moved is one track that ends where the other
   starts; those two don't overlap, so the band stays whole.
3. **Split or not.** A unit is *scheduled* when `match_cadence` finds a run of
   three or more dates. The group splits when tracks were split, or when a
   scheduled unit has other charges of the group inside its run's date span
   (extras alongside a schedule: top-ups, orders, a second income stream). When
   it splits, the bills are the scheduled units, and every other charge is
   ordinary spending. When it doesn't split, the whole group is one bill, exactly
   as today: the 25% minor-charge filter, then cadence, stability and the LLM or
   review fallback.

The "extras inside the span" test keeps a variable bill whole, such as a
utility at $80 one month and $170 another. Its amounts replace each other
month to month instead of arriving alongside, so it keeps today's handling.

Each bill then runs the existing per-group logic unchanged: cadence, stability,
the LLM habit/bill check, staleness, revival and missed windows.

### 3. Series identity

- Migration `0007` drops `UniqueConstraint("merchant", "direction")` on
  `recurring_series`.
- **Matching a bill to a series**, bills taken largest-first. A bill takes the
  first of these that applies:
  1. *Tags:* the unclaimed series, of the same merchant and direction, that tags
     the bill's newest already-tagged charge. When pre-merge series collapse,
     the live one survives.
  2. *Amount fallback:* otherwise, the unclaimed series of that merchant and
     direction whose `expected_cents` is nearest to the bill's median, provided
     it is within 20%.
  3. *New:* otherwise, a new series.

  Each series is claimed by at most one bill per run.
- **Folding.** If a bill's charges were tagged to a series that no bill claimed,
  that series is folded into the series the bill claimed: its `SeriesEvent`
  rows and `price_change` payloads are re-pointed, its remaining tags are
  cleared, and it is deleted. This is how two pre-merge series collapse into one.
- **Tags.** A bill tags its charges. A charge tagged to a series claimed this run,
  but outside that series' bill, is untagged, which removes top-ups from a
  subscription. Charges of unclaimed, unfolded series keep their tags, as today,
  so revival semantics are unchanged.
- Price changes within a band keep flowing through `events.py`.

### 4. Review decisions per bill

- A `recurring_cluster` payload may carry `amount_cents` (unsigned). The rule:
  a question or decision names an amount **only when its merchant has more than
  one bill** at the time. Detection adds it when the group split; the
  `/recurring/{id}` endpoints add it when the series' merchant and direction
  have more than one series. Otherwise the payload is merchant-level, as today.
  This avoids re-asking about a variable bill each time its median drifts.
- `recurring_cluster_item(merchant, direction, amount_cents=None)` adds the
  amount to the question text when present:
  `Is 'Acme Fitness' ($49.99) a recurring bill?`.
- `models.bill_ref(payload)` returns `(merchant, direction, amount | None)`.
  `models.same_bill(a, b)` is true when merchant and direction match and either
  amount is `None`, or the two amounts are within 20% of the larger. Every
  consumer uses these two functions instead of comparing `(merchant, direction)`.
- **Precedence** among resolved decisions covering one bill: amount-specific
  beats merchant-level, then the highest id wins. Marking a merchant's orders
  "not a bill" therefore can't suppress its subscription once both have their
  own answers.
- Consumers that move to `same_bill`:
  - `detect_recurring`'s force map and open-question set;
  - `apply_resolution`'s human-decision guard and its series effects, which
    end or flag only the series the decision covers;
  - `autoreview_items`' guard;
  - `verify_series`' ledger check;
  - `review_context` samples, filtered to the bill's amounts;
  - `transaction_overlap`'s per-transaction recurring decisions and
    `reviewed_merchant_occurrences`;
  - `reconcile`'s supersede check;
  - `api._series_ledger_item`.

  `human_recurring_keys` becomes `human_recurring_refs`, which returns bill refs.

### 5. Migration `0007`

Schema only: drop the unique constraint (SQLite batch mode). Legacy payloads
have no amount, so they stay merchant-level and need no backfill. Downgrade
restores the constraint and fails loudly if duplicates exist.

## Data flow

The sync order becomes: fetch → ingest → normalize → **renormalize (rules +
merge)** → provider overlap → transfers → financing → auto-review →
recurring (split into bills, match, fold) → verify → events. `reconcile`'s own
call to `detect_recurring` inherits the new grouping. `/normalize/rerun` runs the
same pass.

## Testing

Unit tests, using synthetic data only:

- `rule_normalize` for each new strip rule;
- the merge pass: the multi-token rule, the place rule, product names kept apart,
  manual aliases, series and payload renames;
- bands: a subscription with irregular orders gives one series with orders
  untagged, and a variable bill stays one bill;
- tracks: concurrent same-price subscriptions give two series, and a moved
  billing day gives one;
- a payer's two streams at different amounts give two series;
- folding two pre-merge series;
- precedence between merchant-level and per-bill decisions;
- an idempotent second sync;
- the migration.

Acceptance runs on a rehearsal copy of the live DB (private, not committed):

- every previously split merchant collapses as intended;
- every multi-stream merchant stays split;
- fixed-cost and income totals change only by the removed double counts;
- the open-review count does not balloon;
- the second sync is a no-op.

Only after that, the live DB is backed up and synced.

## Known limitations (later slices)

- Bank-truncated names that are not token prefixes (`Acme Fitnriverton`) stay
  split. An LLM matching slice targets these.
- Descriptors with short per-charge codes (`Acme Energy Ven J2357`) still
  fragment; `_REF_TOKEN` needs 8+ characters.
- A price jump of more than 20% starts a new series, and the old one goes stale.
- A merged name hides the longer descriptor, so a series shows `Acme Fitness`
  rather than `Acme Fitness Riverton`.
