# SimpleFIN + Plaid coexistence: combined history, counted once

**Feature:** Both sources keep syncing with independent physical transaction windows.
Confirmed shared accounts and overlapping observations count once in logical views;
all original observations and older unique history remain available for audit.
**Priority:** high
**Type:** integration

## Prerequisites

- Both providers configured in an isolated test config, with at least one shared
  real-account identity and source-only history. Use synthetic fixtures where possible.
- A fresh private database backup before operational migration; rehearse on a copy.
- Record source IDs, signed cents, dates, descriptions, raw payloads, balance
  observations, holdings quantities/values, and all existing resolved review answers.
- Automated starting point: `uv run pytest -q tests/test_overlap_rollout.py`.

## Test steps

1. Sync both sources. Check `moneta status --json` lists both aggregators and review
   source warnings. Default account count must be logical; `accounts --sources`
   must retain every physical account, provider, suffix, and saved relationship.
2. Check automatic identity only for unique, corroborated institution/currency/type/
   suffix matches. Equal balances alone must not link accounts. Missing/ambiguous
   evidence or conflicting metadata must produce review questions.
3. Resolve an account identity question or use `accounts --link-source ID --to ID`.
   One shared account must now have one selected freshest balance, not a sum.
   Verify ties prefer the root and its saved metadata remains authoritative.
4. Compare default `txns` against `txns --sources` for that account over the full
   retained date range. Overlaps count once; older Plaid-only and newer
   SimpleFIN-only rows remain in default history. Audit duplicates keep their
   original facts and `provider_duplicate` exclusion without changing totals.
5. Check repeated equal purchases within one source retain multiplicity. Conflicting
   merchants, ambiguous counterpart counts, and conflicting reviewed classifications
   remain visible for review. Date offsets beyond four days never suppress; shorter
   offsets require corroboration and uniqueness.
6. Verify power/history/cashflow/networth/obligations agree on logical totals; no
   duplicate recurring stream, payment obligation, or event appears. Two aliases
   of one account cannot form a transfer. Holdings count one available source set;
   a source reporting no holdings does not erase the other. Vesting is retained
   and conflicting annotations are warned about.
7. Run `moneta reconcile --json` twice without upstream changes. Logical reports
   and relationship decisions must remain stable; the second run has no new
   links/suppression/restoration. Raw-money fingerprints and every original
   resolved answer must match before/after. Freshness and sync audit must not advance.
8. Unlink a source account. Its balance/transactions become independently eligible;
   the saved manual decision survives reconciliation. Relink only after inspecting
   the evidence. Invalid/conflicting flags, currencies, cycles, and metadata must
   reject before mutation; alias metadata writes must target the root.
9. Re-sync corrected amounts/dates in controlled fixtures: an invalid duplicate
   must dissolve, affected transfers must be revalidated, and reviewed history must
   survive. Record upstream fact changes separately from reconciliation fingerprints.
10. Check each incremental window still uses its own physical observations, including
    suppressed duplicates. Plaid continues replaying its retained history; a SimpleFIN
    failure cannot be hidden by Plaid's latest transaction. In isolated config,
    simulate one adapter failure (warn + continue) and all-adapter failure (fail
    loudly); restore the config afterwards. Check per-item Plaid login repair too.

## Expected result

Both sources remain connected. Every confirmed real account balance, holdings set,
and overlapping transaction contributes once. Original provider records, original
resolved reviews, and older source-only history are retained. Uncertain identity
remains reviewable, manual controls are reversible, and repeated reconciliation is
stable without pretending to fetch new data.

## Notes

- Aggregates remain single-currency; foreign accounts are explicitly excluded,
  never converted. Missing vesting, balances, borrowing, or loan terms must remain
  disclosed and forecasts provisional.
- A provider fetch can legitimately change observations. Compare fingerprints for
  reconciliation separately from fetch/ingest changes.
- Keep credentials, backups, private fixtures, and generated reports outside git.
  This checklist does not authorize publishing or pushing.
- Plaid item login failures have a separate QA case:
  `plaid-item-login-required-degradation.md`. Repair with `setup plaid-relink`,
  preserving the item rather than minting another same-provider identity.
