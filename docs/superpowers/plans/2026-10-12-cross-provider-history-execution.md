# SDD ledger — plan: docs/superpowers/plans/2026-10-12-cross-provider-history.md

## Execution context

- User requested implementation using subagent-driven development on 2026-10-12; this supersedes the handoff's pending native-execution question.
- Worktree: `~/code/.worktrees/moneta/cross-provider-history`, branch `cross-provider-history`, starting commit `bea881e`.
- Original checkout and its untracked approved documents remain preserved.
- One fresh implementer per task, sequential task reviews, one final whole-branch review. No push or publication.
- Tests use isolated databases. No feature command targets the live database before private-copy validation.
- Task 7 is implemented and reviewed before its operator continuation; final whole-branch review gates private rehearsal and local/live application.

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

- Task 1: complete (commits 4426f46..3ba8128, review clean).
- Task 2: complete (commits 3ba8128..dfe37e5, review clean).
- Task 3: fix round 1 running — metadata validation gap; fix base `aae93ef`, original implementer `/root/task3_accounts` resumed.
- Task 4: pending — transaction observations, links and stale reviews.
- Task 5: pending — logical record integration throughout accounting.
- Task 6: pending — API, CLI and review operations.
- Task 7: pending — documentation, whole-branch review and local rollout.

## Verification

Worktree baseline: 443 tests passed, Ruff lint and format checks passed, strict mypy passed (71 files), no warnings. No product implementation started.

Task 1 implementation: focused RED 21 failed/58 passed, GREEN 79 passed; full suite 463 passed with pristine output, lint/format/strict typing passed (74 files). Controller additionally verified `uv run ruff format --check .` and signed-off commit `3ba8128`. No live data touched.

Task 1 review: `/root/review1_schema` approved spec compliance and task quality, no findings. Its cannot-verify items map to Tasks 2/4/5/7 (logical reports, physical windows, reconciliation and rollout fingerprints); they are outside Task 1's deliverable and will be verified at those gates.

Task 2 Ruling: if the root has no vesting annotation and multiple alias annotations conflict, effective vesting stays unknown and emits a requires-review warning; root annotations otherwise take precedence — preserves evidence without inventing an authoritative quantity — if wrong, reports understate vesting coverage until the conflict is reviewed.

Task 2 implementation: focused RED missing projection, GREEN 21 passed; full suite 484 passed with pristine output, lint/format check/strict mypy passed (76 files). Immutable logical account/holding records select observations without writes; transaction survivors retain physical account IDs and consumers must translate `account_roots`.

Task 2 review: `/root/review2_logical` approved compliance and quality, no findings. Downstream integration/matching/rollout checks are mapped to Tasks 3–7; controller confirmed the full gate and signed-off commit from the implementation report and git history.

Task 3 implementation: initial RED 26 failures, GREEN 26 passed, self-review regressions fixed and final GREEN 32 passed. Full suite 516 passed with pristine output, lint/format check/strict mypy passed (78 files). Automatic evidence reviews and explicit chronological decisions flush without commit; caller rollback preserves atomicity. Source observations remain untouched.

Task 3 review: compliance failed and quality needs fixes. Important: explicit/automatic links accept conflicting saved types, promotion dates and financing metadata; original implementer is adding pre-mutation group validation and regressions. API/CLI and transaction rebuilding checks remain mapped to Tasks 4–6.

Task 3: minor (deferred): account_overlap.py:245 retains open questions after all identity candidates disappear (e.g. corrected distinct masks). Carry to subsequent reconciliation work and final whole-branch review.
