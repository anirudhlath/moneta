# Transfer-linking edge cases in the greedy dedup pass

## Summary
`link_transfers` (`src/moneta/pipelines/transfers.py`) uses a greedy,
confidence-ordered pass over candidate outflow/inflow pairs. Several edge
cases in that greedy algorithm and its API surface can lose transactions
silently, mis-link them, or 500 the server. Consolidated here rather than
filed individually since none block v1 correctness for the common case.

## Context
All in `src/moneta/pipelines/transfers.py` unless noted:

- **Greedy loser gets neither link nor review.** `ordered` is processed
  confidence-group by confidence-group; within each group, candidates whose
  inflow/outflow was already `used` by an earlier (higher-confidence) group
  are filtered out (line ~86). If that empties `cands`, the loop does
  `continue` — no `TransferLink` is created *and* no `ReviewItem` is opened,
  since the review-item branch only runs when `cands` is non-empty. The
  outflow silently gets no resolution path at all.
- **Originally-ambiguous outflow can auto-link as "rule" after rivals are
  consumed.** An outflow that started with 2+ candidate inflows (too
  ambiguous for the `len(cands) == 1` auto-link rule) can end up with
  exactly one remaining candidate once rivals in earlier groups consume the
  others — at which point it auto-links as `LinkMethod.rule` with
  `confidence >= _AUTO_LINK`, even though the original set was ambiguous.
  This may be the right call in practice (last-candidate-standing) but
  isn't a deliberate design decision today, and confidence was computed
  against the original candidate set, not re-scored.
- **Transfer_pair re-resolve inserts a duplicate `TransferLink` → 500.**
  `POST /review/{id}/resolve` (`src/moneta/api.py`) always does
  `session.add(TransferLink(...))` for a `transfer_pair` resolution with no
  check for an existing link on that outflow/inflow. `TransferLink.outflow_id`
  and `.inflow_id` are both `unique=True`; resolving the same item twice (or
  resolving after a link already exists another way) raises an
  `IntegrityError` that isn't caught, surfacing as a 500.
- **Non-atomic `accounts --set-type` + `--set-promo`.** `cli/main.py::accounts`
  fires the `--set-type` PATCH request before validating the `--set-promo`
  date. If the date is invalid, the command exits 1 *after* the type change
  has already been applied server-side — a partial, non-atomic mutation.

## Acceptance criteria
- Greedy losers get a `ReviewItem` (or some explicit resolution path)
  instead of silently disappearing.
- Decide and document whether last-candidate-standing auto-linking is
  intended; if so, re-score confidence against the reduced candidate set.
- `POST /review/{id}/resolve` for `transfer_pair` checks for an existing
  link on the outflow/inflow before inserting, returning a clean 4xx
  instead of a 500 on conflict.
- `accounts --set-type`/`--set-promo` either validates both before mutating
  either, or documents/accepts the partial-application behavior explicitly.
