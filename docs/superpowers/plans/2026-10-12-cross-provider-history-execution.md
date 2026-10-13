# SDD ledger — plan: docs/superpowers/plans/2026-10-12-cross-provider-history.md

## Execution context

- User requested implementation using subagent-driven development on 2026-10-12; this supersedes the handoff's pending native-execution question.
- Worktree: `~/code/.worktrees/moneta/cross-provider-history`, branch `cross-provider-history`, starting commit `bea881e`.
- Original checkout and its untracked approved documents remain preserved.
- One fresh implementer per task, sequential task reviews, one final whole-branch review. No push or publication.
- Tests use isolated databases. No feature command targets the live database before private-copy validation.

## Preflight consistency scan

| Tasks | Producer / consumer or internal consistency | Finding |
| --- | --- | --- |
| 1 | Nullable schema, parser, adapters, ingest, migration parity tests | Consistent |
| 2 | Projection fields and preservation/invalid-graph tests | Consistent |
| 3 | Conservative identity, explicit controls and durable decisions | Consistent |
| 4 | Matching, reconciliation counts and review/link cleanup | Consistent |
| 5 | Projection integration, physical sync windows and accounting tests | Consistent |
| 6 | Shared controls, rebuild routes and CLI validation tests | Consistent |
| 7 | File-database validation, docs, final review and private/live rollout | Consistent; final review precedes live application |
| 1, 2 | Schema relationships feed read-only projection | Compatible |
| 1, 3 | Mask and graph fields feed account grouping | Compatible |
| 1, 4 | Duplicate fields and correction invalidation feed matcher | Compatible |
| 1, 5 | Physical source fields retained for sync windows | Compatible |
| 1, 6 | New review kinds feed resolution routes | Compatible |
| 1, 7 | Additive migration validated on frozen file database | Compatible |
| 2, 3 | Root resolver consumed by linking validation | Compatible |
| 2, 4 | Survivor/root graph semantics match duplicate validation | Compatible |
| 2, 5 | Logical account/transaction/holdings projection consumed everywhere | Compatible |
| 2, 6 | Logical default and physical detail listings | Compatible |
| 2, 7 | Projection reports preserve original financial observations | Compatible |
| 3, 4 | Confirmed account groups bound transaction matching | Compatible |
| 3, 5 | Account identity precedes downstream pipeline rebuilding | Compatible |
| 3, 6 | Shared mutators and durable decisions exposed through controls | Compatible |
| 3, 7 | Conservative proposals inspected during private rehearsal | Compatible |
| 4, 5 | Reconciliation precedes transfers; rebuild owns no fetch/freshness | Compatible |
| 4, 6 | Shared transaction mutator and OverlapStats exposed through review/API | Compatible |
| 4, 7 | Matching preserves raw-money fingerprints; idempotence tested | Compatible |
| 5, 6 | Rebuild operation used by local reconcile route | Compatible |
| 5, 7 | Shared report totals and historical union checked during rollout | Compatible |
| 6, 7 | Completed CLI/API used by documented private/local operations | Compatible |

## Task status

- Task 1: pending — source identity metadata and additive schema.
- Task 2: pending — shared logical projection.
- Task 3: pending — conservative account grouping and reversible controls.
- Task 4: pending — transaction observations, links and stale reviews.
- Task 5: pending — logical record integration throughout accounting.
- Task 6: pending — API, CLI and review operations.
- Task 7: pending — documentation, whole-branch review and local rollout.

## Verification

Worktree baseline: 443 tests passed, Ruff lint and format checks passed, strict mypy passed (71 files), no warnings. No product implementation started.
