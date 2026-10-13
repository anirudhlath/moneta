# Cross-provider history implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Native execution is recommended for these tightly coupled changes, with one fresh whole-branch reviewer at the end.

**Goal:** Combine SimpleFIN and Plaid history for the same real accounts without double-counting balances, transactions, recurring streams, or payment obligations.

**Architecture:** Keep provider account and transaction observations intact, and add canonical-account and duplicate-transaction relationships. A shared logical projection supplies consistent records to every accounting view and downstream pipeline. Reconciliation runs after normalization, before transfers, and records conservative automatic decisions or explicit review requests.

**Tech stack:** Existing Python 3.13+, SQLAlchemy async, SQLite, Alembic, Pydantic, FastAPI, Typer, pytest, Ruff, and mypy. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-12-cross-provider-history-design.md` (user approved).

## Global constraints

- Both sources must keep syncing; older Plaid history remains available in reports.
- Preserve every provider ID, raw payload, date, signed amount, description, account balance observation, and holding quantity/value.
- Account identity requires corroboration; matching balances alone are not proof.
- Compare duplicate transactions only across providers inside a confirmed logical account; never collapse repeated purchases within one provider.
- Posting-date offsets are at most four calendar days and require additional corroboration and a unique counterpart.
- Conflicting reviewed classifications require review rather than silent replacement.
- Keep per-source incremental windows based on physical observations, including duplicates.
- Money remains integer cents; the LLM supplies classifications only.
- Pipelines own commits; shared projections and views are read-only.
- Back up financial data, validate on a private copy, and compare raw-money fingerprints before live reconciliation.
- No credentials, private financial fixtures, or generated reports enter git. No GitHub push or publishing is authorized.

## Review focus

1. Reused suffixes, contradictory card products, or ambiguous same-bank siblings must not be auto-linked (Task 3).
2. Cyclic/dangling canonical relationships must fail visibly rather than double-count or silently lose a record (Tasks 2 and 4).
3. Repeated equal purchases and competing cross-source candidates must keep multiplicity and require review when ambiguous (Task 4).
4. Corrected observations and a failed/stale provider must revalidate links while preserving source windows and reviewed decisions (Tasks 4 and 5).
5. An empty holdings source, conflicting vesting annotations, and posting dates across a month boundary must preserve evidence and single-counting (Tasks 2, 4, and 5).

## File boundaries

- `models.py`, `db.py`, migration `0006_provider_overlap.py`: additive schema and review kinds.
- `aggregator/base.py`, `aggregator/simplefin.py`, `aggregator/plaid.py`, `pipelines/ingest.py`: source mask metadata and correction invalidation.
- New `identity.py`: institution/suffix normalization and conservative descriptor compatibility.
- New `logical.py`: canonical maps, effective accounts, survivors, selected holdings.
- New `pipelines/account_overlap.py`: automatic account proposals, explicit link/unlink validation, durable decisions.
- New `pipelines/transaction_overlap.py`: one-to-one observation matching, survivor choice, derived-link and review cleanup.
- New `pipelines/reconcile.py`: the reconciliation coordinator and `OverlapStats`.
- Existing `queries.py`, pipeline stages, `vesting.py`, and `views/*`: consume the logical projection.
- Existing `api.py`, `cli/main.py`, `pipelines/review.py`: account controls, reconciliation command, overlap review contexts/resolutions.
- New focused tests below; existing factories gain optional identity fields without changing defaults.
- README, user guide, PRD, coexistence QA: final behavior and operator instructions.

## Verification and commits

Start execution in isolation using the installed worktree skill and the user's declared `~/code/.worktrees/<project>/<branch>/` convention. Inspect existing attachments before creating one; use native creation if it supports the selected repository/location. Preserve the current checkout and its uncommitted approved documents. Verify the code baseline before Task 1 (currently 443 passing tests, clean lint/format/mypy).

Each task follows RED -> GREEN. Format changed code, then run this full gate before its signed-off commit:

```bash
uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src tests
```

Expected: all tests pass with no warnings; Ruff passes; strict mypy reports no issues. Use `git commit -s`; do not add AI coauthor trailers. Keep the approved spec, plan, and execution ledger in the same branch.

### Task 1: Source identity metadata and additive schema

**Files:** Modify `src/moneta/models.py`, `src/moneta/db.py`, `src/moneta/aggregator/base.py`, both adapters, `src/moneta/pipelines/ingest.py`, and `tests/factories.py`; create `src/moneta/identity.py` and `src/moneta/migrations/versions/0006_provider_overlap.py`; test `tests/test_overlap_schema.py`, `tests/test_migrations.py`, `tests/test_simplefin.py`, `tests/test_plaid.py`, `tests/test_ingest.py`.

**Interfaces:** Produce `Account.mask: str | None`, `Account.canonical_account_id: int | None`, `Transaction.duplicate_of_id: int | None`, `AccountDTO.mask: str | None = None`, and ReviewKind values `account_overlap` / `transaction_overlap`. Produce `identity.infer_mask(name: str) -> str | None`. All new relationships default to `None`; migration revision is `0006`, down revision `0005`, and `db._HEAD == "0006"`.

- [ ] Write `test_overlap_columns_preserve_existing_observations`: upgrade a frozen 0005 database; assert unchanged account balances and raw transaction fields, nullable relationships, and correct migration head. Add `test_mask_parsing_is_conservative` asserting `"Checking (1234)" -> "1234"` and generic/nondigit suffixes -> `None`. Add adapter/ingest tests proving masks round-trip and same-source corrections clear affected duplicate relationships as well as stale transfer links.
- [ ] Run `uv run pytest -q tests/test_overlap_schema.py tests/test_migrations.py tests/test_ingest.py tests/test_simplefin.py tests/test_plaid.py`. Expected RED on missing fields/parser/migration.
- [ ] Add the columns, chained migration, and review kinds, using SQLite-compatible batch alteration for self-referencing foreign keys. Parse Plaid's `mask` and unambiguous SimpleFIN trailing four-digit suffixes. Backfill only inferable masks from saved names with frozen migration-local parsing. Ingest stores mask metadata and invalidates duplicate relationships that depend on a corrected amount/date; retain all provider observations.
- [ ] Run the same tests. Expected GREEN with migration/model parity and existing ingest behavior preserved.
- [ ] Run the full gate and commit these files with `git commit -s -m "feat: retain provider identity and overlap metadata"`.

### Task 2: Shared logical projection

**Files:** Create `src/moneta/logical.py`; test `tests/test_logical.py`; reuse factories.

**Interfaces:** Consume Task 1 fields. Produce `resolve_account_roots(accounts: list[Account]) -> dict[int, int]` and `load_logical_data(session: AsyncSession) -> LogicalData`. `LogicalData` exposes `accounts: dict[int, LogicalAccount]`, `account_roots: dict[int, int]`, `transactions: list[Transaction]` (survivors), `transaction_roots: dict[int, int]`, `holdings: list[LogicalHolding]`, and `warnings: list[str]`. Effective account fields use root metadata plus one newest balance observation; holding fields retain symbol, quantity, market value, and effective vesting quantities. Existing root vesting annotations take precedence; conflicting annotations stay intact and produce a visible warning requiring review.

- [ ] Write `test_logical_accounts_choose_one_fresh_balance`: two linked checking observations of 100000/120000 cents yield one 120000-cent account; date ties prefer the root and root financing/type/promo decisions survive. Write `test_logical_survivors_keep_older_and_unique_rows` asserting one duplicate is excluded while older-only and same-source equal purchases remain. Write `test_holdings_fallback_preserves_vesting` asserting an empty source does not erase holdings and overlapping holdings are counted once. Add tests rejecting cyclic/dangling relationships and flagging conflicting vesting annotations without silently choosing a new quantity.
- [ ] Run `uv run pytest -q tests/test_logical.py`. Expected RED for the missing projection.
- [ ] Implement the projection without writes or commits. Flatten valid roots, select the newest balance by date with stable root-source ties, preserve source metadata, and select one available provider holdings set per logical account. Validate duplicate targets belong to the same logical account and resolve to live survivor rows.
- [ ] Run the focused tests. Expected GREEN; raw rows and their monetary values are unchanged.
- [ ] Run the full gate and commit with `git commit -s -m "feat: project logical accounts and transaction observations"`.

### Task 3: Conservative account grouping and reversible controls

**Files:** Create `src/moneta/pipelines/account_overlap.py`; extend `src/moneta/identity.py`; test `tests/test_account_overlap.py`.

**Interfaces:** Consume `resolve_account_roots`. Produce `identity.normalize_institution(name: str) -> str`, `propose_account_links(session: AsyncSession) -> int`, and `set_account_link(session: AsyncSession, source_account_id: int, canonical_account_id: int | None) -> None`. Proposals flush changes but leave the commit to the coordinator. Explicit decisions use review payload `{"account_id": ID, "candidates": [ROOT_ID, ...]}` and resolution `{"canonical_account_id": ROOT_ID | null}`.

- [ ] Write `test_same_suffix_distinct_sources_form_one_group` using matching institution, currency, compatible type, and mask across SimpleFIN/Plaid; assert one root and both original balances retained. Add `test_equal_balances_do_not_establish_identity`, `test_ambiguous_or_missing_mask_needs_review`, `test_same_mask_conflicting_products_needs_review`, and explicit-control tests rejecting cycles/currency conflicts and preserving existing user decisions. Write `test_manual_unlink_survives_next_reconciliation`: the last unlink decision prevents automatic relinking.
- [ ] Run `uv run pytest -q tests/test_account_overlap.py`. Expected RED for missing linking/proposal behavior.
- [ ] Implement conservative institution normalization (case/spacing and corroborated bank aliases), suffix/type/product checks, and unique cross-provider candidates. Record automatic links with their evidence; open review for ambiguous candidates. Explicit link/unlink validates the root graph and records durable decisions in chronological review history; preserve root metadata and retain source rows. Matching balances alone never trigger a link.
- [ ] Run the focused tests. Expected GREEN, including rejected links leaving the original relationships unchanged.
- [ ] Run the full gate and commit with `git commit -s -m "feat: reconcile shared provider account identities"`.

### Task 4: Transaction observations, links, and stale reviews

**Files:** Create `src/moneta/pipelines/transaction_overlap.py` and `src/moneta/pipelines/reconcile.py`; extend `src/moneta/identity.py`; test `tests/test_transaction_overlap.py`.

**Interfaces:** Consume Tasks 1–3. Produce `identity.compatible_descriptors(left: Transaction, right: Transaction) -> bool`, `set_transaction_duplicate(session: AsyncSession, transaction_id: int, duplicate_of_id: int | None) -> None`, and `reconcile_provider_overlap(session: AsyncSession, today: date) -> OverlapStats` (owns its commit). `OverlapStats` fields: `accounts_linked`, `transactions_suppressed`, `transactions_restored`, `reviews_opened`, `reviews_superseded`, all integer counts. Transaction-review payload is `{"transaction_id": ID, "candidates": [ID, ...]}`; resolution is `{"duplicate_of_id": ID | null}`.

- [ ] Write `test_union_counts_overlap_once_and_retains_history`: an older Plaid-only row, a current matched pair, and a unique SimpleFIN row produce three survivors and four intact raw rows. Write multiplicity tests: two equal purchases in each source yield two survivors; two vs three yield three. Distinct named merchants with equal cents/date do not collapse. A corroborated unique date offset of four days can match; five days cannot. Add competing-candidate, reviewed-classification conflict, reversed-arrival, and upstream-correction tests.
- [ ] Add `test_transfer_endpoints_preserve_reviewed_survivors` and `test_suppressed_questions_are_explicitly_superseded`: remap equivalent derived links to survivor IDs without duplicate endpoints, retain stronger reviewed links, preserve existing resolved decisions, and record supersession reasons. Assert identical raw-money fingerprints and all raw records retained.
- [ ] Run `uv run pytest -q tests/test_transaction_overlap.py`. Expected RED for missing reconciliation.
- [ ] Implement one-to-one cross-provider matching inside confirmed account groups, using signed cents, compatible identity, exact dates first, and unique corroborated offsets up to four days. Preserve reviewed observations before stable source/row preferences. Respect explicit rejected pairs. Revalidate flags and transfer links after corrections, retire/repoint stale review requests, and end series supported only by suppressed observations. No raw transactions or source accounts are deleted.
- [ ] Run the focused tests. Expected GREEN, idempotent reconciliation, valid foreign keys, and preserved financial observations.
- [ ] Run the full gate and commit with `git commit -s -m "feat: reconcile overlapping provider transactions without losing history"`.

### Task 5: Integrate the logical record set everywhere

**Files:** Modify `src/moneta/queries.py`, `src/moneta/pipelines/run.py`, transfers, financing, recurring, review, events, `src/moneta/vesting.py`, and `src/moneta/views/*`. Test new `tests/test_overlap_accounting.py` plus the affected existing suites.

**Interfaces:** Consume `load_logical_data` and `reconcile_provider_overlap`. Produce `rebuild_logical_derived(session: AsyncSession, llm: Classifier | None, today: date) -> None` in `pipelines/reconcile.py`; it runs transfers -> financing -> auto-review -> recurring -> verification -> events using logical records, without fetching providers or advancing sync freshness. Extend `SyncReport` with `overlap: OverlapStats`. Add `include_sources: bool = False` to `transactions_report`; source-detail duplicate rows use `excluded_because="provider_duplicate"` and never contribute to totals.

- [ ] Write `test_all_accounting_views_use_the_same_survivors`: assert one balance, one payroll receipt, one bill/purchase, one financing payment and one holdings set across two providers in net worth, power, cash flow, obligations, history, and transaction totals. Assert older-only transactions remain in historical rows. Add fixtures for currency selection by logical account, posting-date offsets across month end, no same-logical-account transfers, and orphaned duplicate-only series retirement.
- [ ] Add `test_repeated_sync_preserves_source_windows_and_manual_decisions`: reversed adapter order and a failed/stale provider preserve windows calculated from physical source observations, latest logical balance selection, review decisions, and stable survivor totals.
- [ ] Run `uv run pytest -q tests/test_overlap_accounting.py tests/test_run.py tests/test_power.py tests/test_cashflow.py tests/test_financing.py tests/test_networth.py tests/test_transactions.py`. Expected RED for double counts/current pipeline behavior.
- [ ] Insert reconciliation after normalization and before transfers. Route every accounting/recurring/event/financing lookup through the logical projection while leaving `_sync_since` physical/source-specific. Update transfer classification to logical account IDs and selected endpoints. Preserve the existing pipeline order and commit ownership. Keep transaction audit source details available and distinguish provider duplicates from internal transfers. Add `warnings: list[str]` with an empty default to `NetWorthReport` and render logical holding/vesting warnings in its CLI output.
- [ ] Run the focused tests plus `tests/test_recurring.py`, `tests/test_events.py`, `tests/test_transfers.py`, and `tests/test_vesting.py`. Expected GREEN with unchanged single-source outputs.
- [ ] Run the full gate and commit with `git commit -s -m "fix: calculate finances from logical accounts and combined history"`.

### Task 6: API, CLI, and review operations

**Files:** Modify `src/moneta/api.py`, `src/moneta/cli/main.py`, `src/moneta/pipelines/review.py`; test new `tests/test_api_overlap.py` and `tests/test_cli_overlap.py`, plus existing API/CLI suites.

**Interfaces:** Consume Task 3/4 mutators and Task 5 rebuilding. Add `POST /accounts/{account_id}/link` with `{"canonical_account_id": ROOT_ID}`, `POST /accounts/{account_id}/unlink`, and `POST /reconcile` returning `OverlapStats`. `GET /accounts?sources=true` lists physical source records with `source`, `mask`, and `canonical_account_id`; default lists logical accounts. Add `sources` query support to the existing transaction endpoint. Status's `accounts` count is logical.

- [ ] Write API tests: default account count/list contain one shared account, source details contain two, alias-ID metadata patches target the root, linking rejects cycles/incompatible currency/boolean IDs, explicit link/unlink and both overlap review kinds persist decisions, and `POST /reconcile` rebuilds estimates without network calls or a fictional fresh-sync timestamp.
- [ ] Write CLI tests: `moneta accounts --sources`, `--link-source ID --to ID`, `--unlink-source ID`, `moneta txns --sources`, and `moneta reconcile --json` send the exact corresponding requests. Invalid/conflicting flags must fail before any mutation. Extend interactive review with numbered account/transaction candidates and explicit “none” choices; existing bill/habit prompts remain unchanged.
- [ ] Run `uv run pytest -q tests/test_api_overlap.py tests/test_cli_overlap.py`. Expected RED for missing endpoints/flags/validation.
- [ ] Implement the routes and flags, using the shared mutators through the existing review-resolution path. Validate candidate membership, type, currency, logical family, exact signed amount and date limits; reject booleans as IDs. Keep legacy read-command `--json` mutation restrictions; the explicit `reconcile` command supports structured results. Show provider supersession context rather than presenting duplicate rows as new purchases.
- [ ] Run focused tests plus `tests/test_api.py tests/test_cli.py tests/test_autoreview.py`. Expected GREEN with old endpoint/CLI behavior preserved for single-source data.
- [ ] Run the full gate and commit with `git commit -s -m "feat: expose reversible provider linking and overlap review"`.

### Task 7: Documentation, whole-branch review, and local rollout

**Files:** Create `tests/test_overlap_rollout.py`. Update `README.md`, `docs/user-guide.md`, `docs/PRD.md`, `docs/qa-backlog/plaid-simplefin-coexistence-sync.md`, the approved spec's completion status, and this plan's checked steps. Keep private rollout scripts/logs and regenerated financial reports outside git.

**Interfaces:** Consume the completed CLI/API and accounting behavior from Tasks 1–6. The local rollout is part of the requested financial-data correction; no push/publish is included.

- [ ] Add `test_reconcile_file_database_preserves_raw_money`: create an on-disk old-schema fixture with shared source accounts, overlapping/older/unique transactions, holdings, and resolved reviews; migrate, reconcile twice, verify raw-money fingerprints, earliest retained date, one logical balance, and unchanged repeat results. Run it RED before completing the rollout harness/documented commands if any required behavior is absent.
- [ ] Document normal dual-source use, automatic versus reviewed identities, source-detail inspection, link/unlink, reconciliation, and correction behavior. Replace the current QA advice to avoid shared banks with explicit single-counting/older-history checks; update PRD and README in the same change.
- [ ] Run the whole-branch review selected by the execution method; address Important/Critical findings with failing regression tests and one verified fix pass. Run the full repository gate once the final source changes are done. Expected all tests, lint, formatting, and strict typing pass without warnings.
- [ ] Commit the ready documentation with `git commit -s -m "docs: document combined provider history and reconciliation"` before local integration, so the tested code and its user guide are applied together. Keep the remaining rollout checkboxes open until their operations succeed.
- [ ] Take a fresh private live backup. Rehearse migration/reconciliation with a private copy, populate available source identities through the normal adapters, and inspect proposed groups/ambiguous matches. Assert every raw financial observation and resolved original review remains present, duplicated balances disappear, and older unique Plaid transactions remain in the default views. Review any uncertain matches rather than forcing them.
- [ ] Apply the verified change locally to the original checkout with a clean, non-forced integration, preserving any user edits and owned draft documents. Migrate and reconcile the live database from the verified inputs; compare before/after raw-money fingerprints for the reconciliation itself. If provider fetching changes actual observations, record those upstream changes separately rather than calling them reconciliation edits.
- [ ] Regenerate the financial report from the live logical records and combined retained history. Disclose source failures, ambiguous matches, missing vesting/loan balances, and provisional forecasts. Verify source records are retained, logical totals agree across views, and repeated reconciliation is stable.
- [ ] Record successful rollout and report verification in the execution ledger and handoff notes, then update the completed plan/spec milestones. If this produces a final documentation-only commit, integrate that commit locally too after the required gate. Keep operational data private; report the local result and any remaining genuine review questions.
