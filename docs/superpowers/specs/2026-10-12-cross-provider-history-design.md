# Cross-provider accounts and historical transaction overlap

Status: approved by the user on 2026-10-12; implementation ready on
`cross-provider-history`, with synthetic migration/reconciliation rehearsal and
documentation complete. Final whole-branch review, private-copy validation, local
integration, live reconciliation, and report regeneration remain pending. The live
database is unchanged by implementation.

## Intended outcome

The user connected Plaid to obtain older history for accounts already connected
through SimpleFIN. Both sources must keep syncing. Reports must combine their
history, count each real account balance once, and count overlapping transactions
once. Keeping only one source's data does not satisfy this requirement.

This is an architectural change to existing ingestion, reconciliation, query,
and review boundaries. It is not a change to transfer deduplication: two provider
observations of one transaction are distinct from the two legs of a money transfer.

## Current behavior and evidence

`pipelines/ingest.py` identifies accounts by `aggregator_id`, and transactions by
`(account_id, aggregator_id)`. The providers give the same account different IDs,
so their observations enter separate account and transaction rows. Reports then
sum both rows. The coexistence QA document explicitly describes this limitation.

Read-only inspection found matching provider accounts with equal currency, balance,
and balance date. Account names and institutions corroborate the relationships.
For several shared accounts, every transaction in the shorter source also appears
in the longer source with the same signed amount and posting date. Plaid supplies
earlier dates that are absent from SimpleFIN, so discarding all Plaid observations
also discards useful history. Matching balances alone are not proof of identity.

Existing ingest and migration tests pass (16 tests). They cover idempotence within
one provider, not a logical account shared between providers.

## Approach and alternatives

Recommended: retain provider records and add explicit logical identity and
duplicate-observation relationships. Select logical records consistently across
the pipeline and reports. This preserves evidence, corrections, and reversibility.

Deleting one source's account loses history or requires rewriting provider IDs and
sync state. Choosing a single provider per account also misses observations found
only in the other provider. Neither approach meets the preservation requirement.

## Account identity

- Add an optional masked account suffix to `AccountDTO` and `Account`. Plaid
  supplies `mask`; SimpleFIN's trailing parenthesized account suffix can be used
  when its format is unambiguous. A missing suffix remains missing.
- Add nullable `Account.canonical_account_id`, referencing a root Account. Roots
  have no parent. Keep each provider ID, source, name, balance, and balance date.
- Automatic proposals require the same currency, a compatible account type, a
  corroborated institution, the same nonempty masked suffix, and exactly one
  compatible account from the other provider. Do not use matching balances or
  generic names as the identity rule. Ambiguous cases require explicit review.
- Support explicit linking and unlinking in the existing account API/CLI. Reject
  self-links, cycles, incompatible currencies, and incompatible user metadata.
- Prefer the existing root so saved classifications and operator-facing account
  IDs survive. Source aliases resolve to the root for type, financing, and promo
  metadata. Preserve explicit user decisions; surface conflicting decisions.
- A logical balance is one source observation, selected by newest balance date.
  On a date tie, prefer the root source. Never sum provider balances for the group.
- Default account listings and status counts show logical accounts; a source-detail
  option exposes provider aliases and their original observations for inspection.

## Transaction observation reconciliation

- Add nullable `Transaction.duplicate_of_id` referencing the selected observation.
  Preserve every provider transaction ID, raw payload, date, signed amount,
  description, and account reference. Do not delete or overwrite financial facts.
- Only compare transactions from different providers within a confirmed logical
  account. Never collapse repeated transactions from the same provider.
- Match one-to-one using signed integer cents, currency, posting date, and compatible
  merchant/payment identity. Exact-date matches are preferred. An offset of at most
  four calendar days needs additional corroboration and a unique counterpart.
- Repeated equal purchases require multiplicity-preserving one-to-one matching.
  Equal amounts and dates with conflicting merchant identities are insufficient.
  Uncertain cases remain visible and go to review; do not silently suppress them.
- Prefer an existing reviewed observation, then the root provider, then a stable
  row-ID order. Preserve merchant and series classifications on the survivor.
  Conflicting reviewed classifications require review rather than silent replacement.
- Transactions without a counterpart remain logical transactions. In particular,
  older Plaid history remains available in cash flow, history, and transaction views.
- Recompute reconciliation when upstream amounts or dates change. A correction
  can dissolve a former duplicate relationship; the affected transfer links must
  be revalidated. Repeated syncs are idempotent and arrival order is tested.

## Pipeline and query boundaries

Sync order becomes fetch -> ingest -> merchant normalization -> provider-overlap
reconciliation -> transfer linking -> financing detection -> auto-review ->
recurring detection -> verification -> events. Reconciliation owns its commit.

Keep `_sync_since` based on physical observations and their actual source, including
duplicate observations. One provider's longer history must not advance the other's
incremental window or hide a failed source.

Shared query helpers define logical accounts, effective balances/types, transaction
survivors, and logical transfer endpoints. Use them in power, cash flow, net worth,
obligations, transaction drill-down, recurring detection, events, financing detection,
and transfer matching. Suppressing duplicates only in a report is insufficient.

Transfer matching must reject movement between two provider aliases of the same
logical account. Reconcile existing transfer endpoints to survivor transaction IDs,
retain the strongest existing reviewed link when equivalent links collide, and
record which derived links were superseded. Keep raw observations and review history.

End or recompute series whose only contributing observations were suppressed.
Preserve resolved recurring/bill/habit decisions. Review items about suppressed
observations should show their survivor or be retired with an explicit supersession
reason; they must not remain misleading duplicate questions.

Holdings must also be counted once per logical account. Choose one provider's
reported holdings set; a provider that supplies no holdings must not erase the
other provider's holdings. Preserve imported vesting data. This feature does not
infer vesting, margin borrowing, interest rates, or missing account balances.

## Operational rollout

1. Implement and test in an isolated branch, keeping the existing checkout intact.
2. Add a chained Alembic migration and update `db._HEAD`; existing rows default to
   unlinked accounts and unsuppressed transactions.
3. Take a fresh owner-only backup before applying the migration to financial data.
4. First run reconciliation against a private database copy. Check confirmed account
   groups, duplicate matches, the retained older date range, and the original review
   decisions before applying equivalent operations to the live database.
5. Require identical raw-money fingerprints before and after reconciliation: account
   observations, transaction amounts/dates/descriptions/IDs/raw data, and holdings
   quantities/values must remain unchanged. Only bookkeeping relationships change.
6. Refresh the full-history reports using logical records, including older unique
   Plaid data. Report unresolved identity/overlap cases and provider fetch warnings.

No credentials, raw private financial fixtures, or generated financial reports enter
git. Local application of the tested change does not authorize publishing or pushing.

## Verification

Regression tests must fail before implementation and cover:

- two providers for one account count one balance, retain both source observations,
  and use the freshest balance without losing root-level user classifications;
- both older-only and newer-only transactions survive, while overlapping observations
  count once in income, spend, fixed costs, cash out, and transaction audit views;
- repeated same-day equal purchases keep their multiplicity, distinct same-amount
  merchants are not falsely merged, and missing/ambiguous masks require review;
- duplicate checking/card observations do not create false transfers, duplicate
  payment obligations, or duplicate recurring streams/events;
- corrections, link/unlink, repeated syncs, reversed provider arrival order, and
  per-source incremental windows work without losing financial observations;
- holdings and vesting remain single-counted across provider aliases;
- migration/model parity, API validation, source-detail CLI output, and old
  single-source behavior remain correct.

Run the affected tests during implementation, then the complete repository checks:
pytest with pristine output, Ruff lint/format, and strict mypy. Update README,
the user guide, PRD, and coexistence QA expectations with the shipped behavior.
