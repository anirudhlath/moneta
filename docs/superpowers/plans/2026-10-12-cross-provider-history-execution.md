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
| 5 | Projection integration, physical sync windows and accounting tests | CLI warning rendering is explicitly required; ownership includes cli/main.py despite its omitted file-list entry |
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
- Task 3: complete (commits dfe37e5..25785fd, review clean; two deferred minors).
- Task 4: complete (commits 25785fd..fae06d5, review clean).
- Task 5: fix round 2 running — base `03cd10d`, original implementer `/root/task5_integration` resumed for adjudication-history regression.
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

Task 3 fix 1 implementation: metadata RED 13 failures; focused GREEN 51 passed; full suite 535 passed with pristine output, lint/format check/strict mypy passed (78 files). Group-wide types/promotion/recorded-financing decisions validated before mutation. Current historical PATCH defaults have no provenance; Task 6 must record future explicit financing PATCH decisions via existing financing review history. No schema expansion or inference of historical intent.

Task 3: fix round 1/5 (1 addressed, 0 open; metadata guard verified, commits aae93ef..25785fd). Scoped reviewer `/root/rereview3_metadata` found no Critical/Important breakage; spec and task-quality gates now pass.

Task 3: minor (deferred): account_overlap.py:295–296 can show metadata_conflicts from alternatives later excluded by manual protection; refresh conflict context from retained candidates. Carry to final review.

Task 4 implementation: initial RED 28 expected failures, GREEN 28 passed; three self-review regressions fixed with RED/GREEN evidence, final focused GREEN 38 passed. Full suite 573 passed with pristine output, lint/format check/strict mypy passed (81 files). Raw account/transaction/holding fingerprints, FK checks, replay/idempotence and caller rollback tests pass. Manual duplicate resolution appends observation evidence; historical resolved items stay unchanged.

Task 4 review: `/root/review4_transactions` reported spec noncompliance and quality needs fixes. Important findings: survivor ordering must be reviewed status → root provider → stable row ID; explicitly rejected duplicate inflows must preserve distinct reviewed transfer occurrences; derived alias links must not override a survivor's manual no-transfer decision. Regression/fix wave dispatched together to the original implementer. Cross-task integration/window/rollout items map to Tasks 5–7.

Task 5 integration check: existing link_transfers' high-confidence path must honor resolved no-transfer choices after transaction IDs map to survivors; otherwise it can recreate a link removed by Task 4 cleanup. Add repeated-sync/rebuild regression for reviewed rejections on either provider observation. Account-filtered transaction drill-down must include alias-only historical survivors via account_roots.

Task 4 fix 1 implementation: RED 9 failures, focused GREEN 46 passed; full suite 581 passed with pristine output, lint/format check/strict mypy passed (81 files). Survivor ranking corrected; reviewed transfer endpoint equivalence uses confirmed or mutually unique occurrence identity with durable rejections; latest human no-transfer decisions dominate derived links on either provider. Shared pure `reviewed_transfer_choices(list[ReviewItem]) -> dict[int, int | None]` exported for Task 5.

Task 4: fix round 1/5 (3 addressed, 0 open; commits dcacc30..fae06d5). Scoped reviewer `/root/rereview4_decisions` found no new breakage, both gates pass. Existing transfer detection's no-transfer handling remains explicitly owned by Task 5.

Task 5 diagnostic: confirmed that corrected alias currency can supply the selected balance under the root's currency label because existing group currency was validated only when linking. Minimal logical.py same-currency validation and a failing regression are included under the approved account-identity invariant; accounting must not implicitly convert money.

Task 5 implementation: initial RED 8 failures/78 passes; subsequent integration and manual transfer-priority regressions observed RED before their changes. Final focused GREEN 194 passed; full suite 598 passed with pristine output, lint/format check/strict mypy passed (82 files). Shared accounting, recurrence/events/review/financing consumers now use logical data; source fetch/ingest/normalization and physical _sync_since unchanged. Rebuild is network-free with classifier None and writes no sync freshness. Controller recovered the truncated report's full gate/sign-off evidence before dispatching review.

Task 5 review: `/root/review5_integration` reported spec noncompliance and quality needs fixes. Important: alias-to-survivor dictionary overwrites can replace human transfer rejection with LLM acceptance and misattribute method; a logical currency vote flip can mix old-currency series cents into new-currency power totals. Both are under a combined regression/fix wave. Public controls/rollout checks remain mapped to Tasks 6–7.

Task 5 fix 1 implementation: 10 transfer/currency RED regressions plus stale legacy/modern price-request and forward-only cursor regressions observed before changes. Final focused GREEN 198 passed; full suite 614 passed with pristine output, lint/format check/strict mypy passed (82 files). Survivor-level choice reduction preserves human precedence/provenance and opens human-only equal-strength conflicts. Foreign series retain state while monetary consumers filter eligibility; price proposals now carry currency/transaction evidence and reject stale denomination. Raw observations and original resolved review records remain unchanged.

Task 6 required handoffs: record future explicit financing PATCH choices through existing review history and settle related open questions; catch apply_resolution's stale-currency ValueError as a client validation response. Shared effective_transfer_choices now returns survivor-level choices/provenance/conflicts; human conflict review payload includes transfer_review_ids and resolutions adjudicate them without changing old records.

Task 5: fix round 1/5 (2 addressed, 1 open; commits b0e41ee..03cd10d). Scoped review `/root/rereview5_currency` verified original fixes but found Important new breakage: removing named adjudicated rows before latest-per-observation selection resurrects older superseded answers and removes the accepted link. Original implementer resumed with a multiple-answer regression and repeated-rebuild check.
