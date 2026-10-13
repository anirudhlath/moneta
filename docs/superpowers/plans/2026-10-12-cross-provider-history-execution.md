# SDD ledger — plan: docs/superpowers/plans/2026-10-12-cross-provider-history.md

## Execution context

- User requested implementation using subagent-driven development on 2026-10-12; this supersedes the handoff's pending native-execution question.
- Worktree: `~/code/.worktrees/moneta/cross-provider-history`, branch `cross-provider-history`, starting commit `bea881e`.
- Original approved drafts are preserved in owner-only private archives; the original checkout is now locally integrated on `main`.
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
- Task 5: complete (commits fae06d5..b1d5e63, review clean).
- Task 6: complete (commits b1d5e63..225c4eb, review clean).
- Task 7: complete — documentation/on-disk regression, whole-branch reviews/fixes, fresh private backup/rehearsal, non-forced local integration, verified live reconciliation, and full logical-union reporting completed; final documentation applied locally.

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

Task 5 fix 2 implementation: RED 2 failures; GREEN 2 passed, final covering set 170 passed; full suite 616 passed with pristine output, lint/format check/strict mypy passed (82 files). Supersession filter now follows latest-per-observation reduction, preventing resurrection. Real tests cover multiple prior human/LLM answers, reversed iteration, accepted-link stability, future new human choices and unchanged source facts/history.

Task 5: fix round 2/5 (1 addressed, 0 open; commits 03cd10d..b1d5e63). Scoped reviewer `/root/rereview5_adjudication` found no new breakage; compliance and quality gates pass. All public API handoffs remain assigned to Task 6.

Task 6 implementation: 31 initial RED failures, six transfer validation RED failures and one stale-alias transfer guard RED, followed by 229 focused and 670 full tests passing; lint, format and strict typing clean (84 files). Signed-off commit `7095dba`; shared durable mutations rebuild locally without fetching.

Task 6 review: `/root/review6_interfaces` found Important cross-contract breakage: stronger transfer currency validation can raise during automatic review when reconciliation leaves a stale currency-corrected candidate. Original implementer is fixing invalid candidate cleanup and safe automatic-resolution rejection with regression coverage; live validation remains Task 7.

Task 6 fix 1 implementation: seven meaningful RED failures then GREEN; 162 focused and 677 full tests passed, clean lint/format/strict typing (84 files). Candidate cleanup/context now enforce transfer compatibility; per-answer validation savepoints leave invalid automatic answers open and preserve prior answers. Signed-off commit `225c4eb`; scoped re-review pending.

Rollout preparation (read-only): live schema `0005`, physical counts, review state and history range recorded in the private rollout notes. Preserve every existing resolved review record. The original checkout's draft documents match commit `4426f46` exactly; no user edits detected.

Task 6: fix round 1/5 (1 addressed, 0 open; commits 7095dba..225c4eb). Scoped reviewer `/root/rereview6_stale` confirmed candidate cleanup, filtered live prompts and per-answer rejection containment; no new Critical/Important breakage. Public interfaces and both task gates now pass.

Task 7 phase A implementation: real frozen `0005` on-disk harness initially GREEN because Tasks 1–6 already supply the required behavior. Independent raw SQL fingerprints, all original reviews/sync audit and default/source API assertions cover migration plus two reconcile/rebuild runs. Documentation, QA, spec and handoff updated coherently; signed-off `d4c8766`. Final full gate: 678 tests passed, clean lint/format/strict typing (85 files), no warnings. Phase A task review pending; whole-branch review and real-data operations remain open.

Task 7 phase A review: `/root/review7_documentation` approved spec compliance and task quality with no findings; checked all docs and Task 6 contract handoff, frozen schema, independent fingerprints and literal logical/source/repeat expectations. No tests rerun. Final whole-branch review now starts over `bea881e..d4c8766`; Task 3's two deferred minors are included for explicit triage.

Final review: `/root/final_branch_review` completed the whole-branch pass with four Important findings (automatic transfer answers override human rejection; alias-only financing/promotion settings disappear; complementary reviewed transaction classifications disappear; conflict-unknown vesting is counted fully vested). All were reproduced on synthetic in-memory data, including two full syncs for transfer oscillation. Three Minors: both deferred Task 3 stale question/conflict-label issues and incomplete source-side CLI overlap context. No Critical finding; one combined fix wave covers all seven before private/live operations.

Final Ruling: preserve a sole explicit alias financing/promotion decision when a group root has uninformed defaults, overriding the literal root-only projection instruction where necessary — the spec's preservation of user decisions is binding — if wrong, the linked account's effective obligation or promotion metadata may need alignment and re-review.

Final review declined-to-judge mapping: real backup/enrichment/private/live/report operations remain mandatory Task 7 phase B after this gate. Ordinary never-annotated holdings and missing loan/margin/interest inference are explicit non-goals; reports retain their limitations. A separate public undo-one-automatic-transaction command is outside the approved surface; the fix wave must keep guide language precise about existing review and account unlink controls.

Final fix wave: `/root/final_preservation_fixes` owns all R1–R4 and M1–M3 in one combined pass, base `d4c8766`; synthetic failing regressions before product fixes, one final full gate and one scoped re-review. Real-data operations remain untouched.

Final fix wave implementation: signed-off `ee6e72c` covers all seven findings. Initial 15 RED preservation failures, independent stale-question/conflict-context failures and later automatic-classification/native-normalization failures observed before fixes; final 35 preservation tests and full 717-test gate pass, with clean lint/format/strict typing (87 files), no warnings. Sole-alias metadata fallback is read-only; explicit promotion clears use existing metadata-only financing history, and no migration was added. Compatible human classifications survive first/repeated sync and restore on unlink/correction; unresolved conflicting vesting value is excluded with an integer-cent provisional warning. Scoped re-review `/root/final_fix_review` pending.

Final scoped re-review: `/root/final_fix_review` marked R1–R4 and M1–M3 addressed with no new Critical/Important breakage. It additionally reproduced an Important inherited implementation edge outside that fix diff: transaction decision comparison ignores transfer-review adjudication supersession, so an explicit reversal splits an otherwise matched pair and counts an extra 1,000 cents of spending on repeat. This is inherited within the new overlap implementation, not ordinary pre-feature behavior.

Rollout Ruling: correct the inherited transfer-adjudication mismatch as a load-bearing prerequisite before applying financial data, with one focused regression/fix check rather than parking it under the final-review wave cap — single-counting and durable explicit decisions are binding requirements, and the developer requires completing authorized work — if wrong, this adds a narrowly scoped verification pass and may require rework of transfer-decision reduction.

Rollout prerequisite implementation: signed-off `43f5c98` extracts the already-working strongest/latest-per-physical then adjudication-supersession reduction into one shared helper consumed by matching and transfer logic. Eight RED reversal scenarios then ten GREEN targeted regressions cover both providers, both directions, local/full/repeated runs, survivor counts and older-answer resurrection. Full 725-test gate passes with clean lint/format/strict typing (87 files), no warnings. Focused review `/root/rollout_adjudication_review` pending; no real-data operations yet.

Rollout prerequisite review: `/root/rollout_adjudication_review` approved the shared reduction and all reversal/survivor/money/history/resurrection regressions, with no new breakage or deferred observations. All source findings are closed at `43f5c98`. Task 7 phase B implementer `/root/task7_rollout` now executes fresh owner-only backup, private-copy/normal-adapter proof, non-forced local integration, verified live application, combined report and final documentation. Stored-only reconciliation will not fabricate source freshness.

Rollout prerequisite review: `/root/rollout_adjudication_review` confirmed the inherited bug addressed and no new breakage; prior seven findings remain closed. Final source/review gate is clean at `43f5c98` (725 tests). Task 7 phase B now resumes for the authorized backup, private rehearsal, non-forced local integration, live reconciliation, report and final documentation evidence.

Task 7 phase B backup/private proof: fresh owner-only SQLite backup API snapshot in a private directory, with integrity/FK checks clean. Private migration to `0006` preserves every raw financial fact and all originally resolved review rows, including exact agreement with the earlier private review audit. Counts, times and paths are kept in the private rollout notes.

Task 7 verified inputs: normal configured adapters fetched once on their physical source windows, without warnings. Frozen owner-only snapshots were replayed identically to private/live ingestion; no repeated provider fetch or LLM classification. Upstream deltas (balance dates only; balance cents and all transaction/holding facts unchanged) were recorded separately. Conservative automatic identities plus explicit uniquely corroborated unknown-type pairs produce the logical accounts. Known-type roots were selected for the explicit pairs without rewriting source metadata/vesting; evidence/root mappings remain private.

Task 7 reconciliation proof: all physical accounts, transaction observations and holdings remain; duplicate observations are suppressed, and default history retains unique older Plaid transactions from before SimpleFIN's coverage, both globally and per matching account. Raw-money fingerprints match before/after reconciliation; all original resolved rows and the earlier review audit remain exact. Full repeated stored state and logical reports are identical, second-run counts all zero, and no SyncRun/freshness was fabricated. No open account/transaction overlap questions remain, and the remaining merchant, recurring and transfer queue was not bulk-reviewed.

Task 7 local application: original checkout rechecked at `bea881e` with only three unchanged drafts matching `4426f46`; archives and displaced originals preserve relative paths/hashes privately. Non-forced `git merge --ff-only cross-provider-history` applied reviewed source `43f5c98` to original `main`. Full gate on the merged original: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src tests` -> 725 tests passed in 34.06s, lint clean, 87 files already formatted, strict mypy clean in 87 source files, no warnings. Verified live migration, frozen replay, and identical identity operations produce the same logical projection as the private rehearsal.

Task 7 full-history report: owner-only HTML/JSON reports outside git, generated from live read-only helpers. All money fields are integer cents; monthly income/spend/cash-out sums match full-history views, account/transaction/holdings counts agree, report values match final live views, and reporting changes no stored state. Observed income receipts are distinct from recurring-model forecasts. Reports disclose source coverage/actual fetch times, open review questions, and data limitations (missing vesting/holdings and loan principal for some accounts, holdings-versus-balance/borrowing uncertainty, brokerage balances omitted without holdings). Desktop and mobile renders show no horizontal overflow. No new dependencies, secrets, financial fixtures, operational scripts or reports entered git. Final documentation gate/commit evidence is in the ignored Task 7 report; no push or publication. Remaining worktree/branch cleanup belongs to the controller.

Task 7 final documentation gate: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src tests` on the implementation branch -> 725 tests passed in 32.33s, lint clean, 87 files already formatted, strict mypy clean in 87 source files, no warnings. Final commit changes documentation only; local final fast-forward verifies identical feature/main commit and trees, unchanged source/tests/dependency lock versus reviewed `43f5c98`, clean statuses, and `git diff --check` without unnecessarily repeating the unchanged source suite.
