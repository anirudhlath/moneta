# Bill Detection Across Spellings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Execution rulings (2026-10-15):**
> - F1: events and `verify_series` judge a bridged bill on all its tagged charges, not on detection's bridged run. Tagged charges are installments only, never deposits. Task 5's "events.py's and review.py's calls unchanged" design and its CLAUDE.md "same charges" sentence are superseded.
> - F2: "no cadence changes" means `match_cadence` is unchanged. `cadence.py` gained helpers (`match_across_a_miss`).
> - F3: Task 1's snippet takes the blank line `ruff format` adds after the nested `glued_target` def. Formatting only.
> - F4: Task 4 Step 2's expected failure names only the four deposits as tagged. The fee is below `whole_bill`'s 25% floor and stays untagged.
> - I1: the miss bridge also needs at most one real-size charge (at least `MINOR_FRACTION` of the unit's median) strictly inside the missed gap, so `_across_a_miss` takes the unit. A habit stays busy through the gap; a missed installment leaves the merchant quiet.
> - M1: known limitation. An off-band charge more than `TOLERANCE` days early counts as an extra, so a bill whose amount changes and posts early may show a missed month. Widening the window would merge term deposits back, so it stays (user guide, Troubleshooting).

**Goal:** A recurring bill gets detected even when its descriptor spellings differ or a
per-charge code hides it, the LLM can call a frequent purchase a habit, not a bill, and a
term deposit beside a monthly installment stays ordinary spending, even after a month the
installment was paid from elsewhere.

**Architecture:**
- `normalize.merge_targets` learns two more folds, each under its own guard. The first is a
  place glued onto a merchant's last word (`Acme Universityelmton` → `Acme University`).
  The second is a letters-only per-charge code (`Acmeapp-Qwertyui` → `Acmeapp`).
  `_ADDRESS` also learns expressway suffixes.
- Auto-review's recurring prompt switches to the three-way taxonomy that detection and
  `verify_series` already use. Both parse answers with one shared helper. A confident
  answer resolves the question exactly the way a human's `b`/`h`/`n` does.
- `bills._scheduled_units` also treats a charge of real size after a schedule's newest
  charge as an extra beside it, while the next charge is not yet due. A term deposit
  after a monthly installment then splits the group: the installment becomes the bill at
  its own typical amount, and the deposits stay ordinary spending.
- `cadence.match_across_a_miss` lets that monthly run continue across one missed month.
  `bills._across_a_miss` guards it, and detection judges a split bill on the same run
  (`newest_run(..., across_a_miss=split)`), so one unpaid month no longer undoes the split.

**Tech Stack:** Python 3.13, SQLAlchemy async + SQLite, pytest (asyncio auto mode), uv.

**Spec:** GitHub issue #41, plus the Background section of this plan. There is no spec
document; Background records each defect, its root cause and the decision taken.

## Global Constraints

- Money is integer cents everywhere. Negative = outflow in `Transaction.amount_cents` and
  `RecurringSeries.expected_cents`.
- LLM output is classification only; it never supplies a money value.
- Pipelines commit; views don't. Enum columns load as plain `str`: compare with `==`.
- No schema change and no migration. The force map, `ReviewItem` payloads and resolutions
  keep their shapes: `{"is_recurring": bool}` plus an optional `"discretionary": True`.
- Merge floors are unchanged: a target needs **4+ letters**. A place is a token that follows
  **3+** existing names. A prefix with **3+** non-place tails is generic.
- New thresholds, used verbatim:
  - a glued tail needs **4+ letters**;
  - a multi-word name needs **2+ different** glued tails;
  - a per-charge code is exactly **8** characters after a hyphen, behind a merchant word
    of **4+ letters**, and needs **2+ different** codes;
  - a charge after a schedule's newest charge is an extra only while **every** charge in
    the group is dated before the next due date minus that cadence's `TOLERANCE`
    (6 days for monthly). No question rule and no cadence changes.
  - a split bill's monthly run may cross **one** missed month: a gap of 2 periods
    ± 2×`TOLERANCE` (**48–72 days**), once per run. It holds only when no charge of the
    merchant lies within `TOLERANCE` (**6 days**) of the missed due date, and no two of
    the unit's dates are closer than a period − 2×`TOLERANCE` (**18 days**).
- The repo is **public**. Use only fictitious examples (Acme, Corner Curry, Elmton) and
  round amounts in code, tests, docs and commit messages.
- Commits use `git commit -s`, with no AI co-author trailers.
- Gate before every commit, all four, with pristine output (a warning is a failure):
  `uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests`.
  `ruff format .` must leave every file unchanged. If it rewrites one, re-add it.
  During red/green steps, run only the narrow test file.
- Work on branch `fix-bill-detection`. Do not close #41: it is a `qa` case, checked against
  real data after merge.

## Review Focus

1. **One longer word beside a name.** `Corner Storehouse` beside `Corner Store` is a
   different shop, not a city glued on. It must not merge, and neither must
   `Acme Parking Monthly` beside `Acme Parking Month`. Pinned in Task 1
   (`test_one_longer_word_is_not_a_glued_place`).
2. **One hyphenated real name.** `Corner-Sandwich`, alone, beside `Corner`, or twice with a
   city, must keep its second word. Pinned in Task 2
   (`test_one_hyphenated_name_is_not_a_code`).
3. **A second sync after a fold** must rename nothing. Pinned in Task 1
   (`test_renormalize_folds_glued_place_descriptors`) and Task 2
   (`test_per_charge_codes_form_one_subscription`): the second `renormalize_merchants`
   returns 0.
4. **An unsure, unknown or old-shape LLM answer** must leave the question open and
   askable, not resolve or flag it. This covers `confident: false`, a label outside the
   three, and the old `{"is_recurring": …}` shape. Pinned in Task 3
   (`test_unsure_or_unknown_classification_stays_open`).
5. **A charge after the schedule** splits it only while the next charge is not yet due.
   A repriced charge that arrives a few days early is the next charge, not an extra. Once
   the group has charged at or after the due window, the old window applies: a purchase
   just after a subscription's newest charge, followed by a price change, keeps one bill.
   Pinned in Task 4 (`test_a_repriced_charge_in_the_due_window_does_not_split`,
   `test_a_later_charge_closes_the_not_yet_due_window`), with the existing
   `test_variable_bill_with_a_small_fee_is_not_split`.
6. **A missed month that wasn't missed** must not bridge a run. A variable bill whose
   month was charged in another amount band (a charge on the due date) was billed that
   month. A habit that repeats a price days apart is no schedule. Pinned in Task 5
   (`test_a_charge_on_the_missed_due_date_is_that_month_not_a_miss`,
   `test_a_habit_that_repeats_a_price_is_no_schedule_across_a_miss`); each fails when its
   guard is removed.

## Background

Each defect below was checked against the code at the base of this branch.

1. **Glued place names.** A feed can glue the city onto the merchant
   (`ACME UNIVERSITYELMTON CA`), so one bill arrives as `Acme University`,
   `Acme Universityelmton` and `Acme Universityriverton`.
   - **Root cause.** `_tokens` (`src/moneta/pipelines/normalize.py:178-180`) splits on
     spaces. `merge_targets` (`:213-228`) merges only a token prefix
     (`toks[:size] in by_tokens`) or an identical token tuple. Places are learned only as
     whole trailing tokens (`:197-201`). `("acme", "universityelmton")` therefore never
     meets `("acme", "university")`.
   - **Decision.** Fold glued places inside `merge_targets`' existing guards, and guard
     against one longer word (Review Focus 1).

2. **Per-charge codes.** `ACMEAPP-QWERTYUI` uses a fresh 8-letter code on every charge.
   - **Root cause.** `_REF_TOKEN` (`normalize.py:35`) strips only 8+ character tokens that
     contain a digit. A letters-only code survives: `rule_normalize` returns
     `Acmeapp-Qwertyui`. Every charge then has its own one-token merchant, and no token
     prefix connects them.
   - **Decision.** Take the safer variant: drop the code only once 2+ different codes
     share the merchant word. One hyphenated name on its own may be real
     (Review Focus 2).

3. **Expressway addresses.**
   - **Root cause.** The `_ADDRESS` suffix list (`normalize.py:29`) has no `expy`/`expwy`.
     So `ACME DINER 4500 OAK HOLLOW EXPY ELMTON TX` normalizes to
     `Acme Diner Oak Hollow Expy Elmton`.
   - **Decision.** Add both suffixes.

4. **Auto-review can't say "habit".**
   - **Root cause.** `_RECURRING_PROMPT` (`src/moneta/pipelines/review.py:76-80`) asks a
     yes/no question. `_validated` (`:499-506`) reads only `is_recurring`, plus a
     `discretionary` key the prompt never asks for. So a confident "yes" about a
     restaurant writes `{"is_recurring": true}`, a fixed cost. Detection
     (`src/moneta/pipelines/recurring.py:306-315`) and `verify_series`
     (`review.py:690-692`) already classify three ways.
   - **Decision.** Use three-way auto-review with `verify_series`' taxonomy, JSON shape
     and parse. A confident habit resolves the same way a human `h` does.
   - **Prompt choice.** The prompt keeps auto-review's own context (merchant, direction,
     recent charges). An open question has no series, so `_VERIFY_PROMPT`'s cadence and
     expected-amount fields have nothing to fill them.
   - **Out of scope.** Questions the LLM already resolved under the yes/no prompt are not
     re-asked.

5. **A payment plan's deposits become part of its bill.** The shape: a fixed monthly
   installment charged from several cards, with some months paid from an account that
   is not linked, plus a deposit of about 1.75× the installment at each term start
   (about three a year), one with a small fee on the same day. The newest stretch is
   three identical installments, then a deposit. This was checked, not assumed, after
   fixes 1–2, with the synthetic version in Task 4's end-to-end test.
   - **Root cause.** `amount_bands` keeps the installments and the deposits in separate
     bands. The installments' newest run, after the last skipped month, matches monthly.
     But the extras window (`bills.py:125-131`) ends at the run's newest charge, so a
     deposit after it is not an extra and nothing splits. The whole group then has a
     cadence and unstable amounts, so `_detect_bill` asks the LLM or opens a
     merchant-level question (`recurring.py:292-315`). A "bill" ruling tags every
     significant charge (`:420`), the deposits included, and `spend_reason`
     (`views/transactions.py:78-79`) counts a charge tagged to an active,
     non-discretionary series as a fixed cost. The deposits vanish from spending.
   - **Decision.** Widen the extras window, guarded (Review Focus 5). While nothing in
     the group has been charged since the next installment came due, a charge after the
     newest one sits beside the schedule. The existing split then makes the installment
     its own series at `_typical_amount`, and the deposits and the fee stay untagged
     spending in the month they land. A deposit never sets or inflates the expected
     amount.
   - **A month paid from elsewhere.** `match_cadence` ends a run at a gap it cannot
     place, so a ~60-day gap leaves only the installments after it. The next installment
     after a missed month therefore has no run of 3: nothing splits, the merchant-level
     question reopens, and a "bill" answer then would tag the deposits. **Decision.** A
     split bill's monthly run continues across one missed month (Task 5), under two
     guards (Review Focus 6). Detection judges the bill on that same run, so it neither
     asks about it nor loses its cadence.
   - **Rejected.** A question rule for varying groups with no cadence at 4–9-week gaps.
     The installment is monthly, so that rule never reaches this shape, and on realistic
     data it asked about ordinary varying spending far more often than about bills. Also
     rejected: crossing missed months without the guards. On realistic data it split a
     variable bill whose month fell into another amount band, made a café a fixed cost
     and split a habit; the guards remove all three.

**Out of scope (noted only):**
- An ended series reviving and claiming new charges, via `_claim`'s tag match
  (`recurring.py:203-204`) and the variable bill's expected-amount refresh (`:376-380`).
- Two installments in a row paid from elsewhere. The run then has no 3 charges until
  three in a row return, so the merchant-level question reopens meanwhile. A "bill" answer
  given then tags the deposits until the plan splits again, which untags them (Task 4's
  `test_a_merchant_level_bill_ruling_never_tags_the_deposits` pins the untagging).
- The missed month itself raises one `missed` event, like any bill whose charge does not
  arrive: Moneta cannot see the account it was paid from.

## File map

| File | Responsibility | Tasks |
|---|---|---|
| `src/moneta/pipelines/normalize.py` | `_ADDRESS` suffixes, `_glued`, `_charge_codes`, `merge_targets` | 1, 2 |
| `src/moneta/pipelines/review.py` | `_RECURRING_PROMPT`, `_classification`, `_cluster_resolution`, `_validated`, `verify_series` parse | 3 |
| `src/moneta/cadence.py` | `match_across_a_miss` | 5 |
| `src/moneta/pipelines/recurring.py` | one comment; `newest_run(..., across_a_miss=...)` | 3, 5 |
| `src/moneta/pipelines/bills.py` | `_scheduled_units` extras window; `_across_a_miss` | 4, 5 |
| `tests/test_normalize.py` | rule and merge-pass tests | 1, 2 |
| `tests/test_autoreview.py` | three-way auto-review tests | 3 |
| `tests/test_bills.py` | extras-window and missed-month split and guard tests | 4, 5 |
| `tests/test_recurring.py` | payment-plan end-to-end tests | 4, 5 |
| `CLAUDE.md`, `docs/PRD.md`, `docs/user-guide.md` | docs upkeep, in each behavior task | 1–5 |

`README.md` needs no change: no command, flag or config key moves. Its LLM section already
lists "irregular recurring clusters" among what the LLM sees.

---

### Task 1: Expressway addresses and glued place names

**Files:**
- Modify: `src/moneta/pipelines/normalize.py:27-31` (`_ADDRESS`), `:171-175` (merge
  constants), `:183-235` (`merge_targets`)
- Modify: `CLAUDE.md` (the "Pipeline order is load-bearing" bullet), `docs/PRD.md:76` and §7,
  `docs/user-guide.md:421-425`
- Test: `tests/test_normalize.py`

**Interfaces:**
- Produces: `merge_targets(names: Iterable[str], targets_only: Iterable[str] = ()) -> dict[str, str]`.
  The signature is unchanged; it now also maps glued names.
- Produces: private helpers `_letters(text: str) -> int` and
  `_glued(keyed: dict[str, tuple[str, ...]], sources: set[str], by_tokens: dict[tuple[str, ...], str]) -> dict[str, tuple[tuple[str, ...], str]]`,
  plus the constants `_MIN_GLUED_LETTERS = 4` and `_MIN_GLUED_TAILS = 2`.
- Task 2 inserts its constants right after `_MIN_GLUED_TAILS = 2`. It also edits the
  docstring sentence and the CLAUDE.md, PRD and history text this task writes.
- Task 4 relies on `ACME UNIVERSITY*` descriptors folding into `Acme University`.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_normalize.py`. `_PLACES`
  is the module's existing list of names that teaches `elmton` as a place.

```python
def test_rule_normalize_strips_expressway_addresses() -> None:
    assert rule_normalize("ACME DINER 4500 OAK HOLLOW EXPY ELMTON TX") == "Acme Diner"
    assert rule_normalize("ACME DINER 4500 OAK HOLLOW EXPWY ELMTON TX") == "Acme Diner"


def test_glued_places_fold_into_the_spaced_name() -> None:
    assert merge_targets(
        ["Acme University", "Acme Universityelmton", "Acme Universityriverton"]
    ) == {
        "Acme Universityelmton": "Acme University",
        "Acme Universityriverton": "Acme University",
    }
    # a one-word name absorbs only a glued place learned from other names
    assert merge_targets([*_PLACES, "Shyco", "Shycoelmton"])["Shycoelmton"] == "Shyco"
    assert merge_targets(["Shyco", "Shycobrook", "Shycofield"]) == {}


def test_one_longer_word_is_not_a_glued_place() -> None:
    # one glued variant is as likely a different word as a place
    assert merge_targets(["Corner Store", "Corner Storehouse"]) == {}
    # a short tail is a word ending, not a place, however many there are
    names = ["Acme Parking Month", "Acme Parking Monthly", "Acme Parking Months"]
    assert merge_targets(names) == {}


async def test_renormalize_folds_glued_place_descriptors(session: AsyncSession) -> None:
    acct = await make_account(session)
    for raw in ("ACME UNIVERSITY CA", "ACME UNIVERSITYELMTON CA", "ACME UNIVERSITYRIVERTON CA"):
        await make_txn(session, acct, description=raw)
    await normalize_merchants(session, llm=None)

    assert await renormalize_merchants(session) == 2
    assert await renormalize_merchants(session) == 0

    txns = (await session.execute(select(Transaction))).scalars().all()
    assert {t.merchant for t in txns} == {"Acme University"}
```

- [ ] **Step 2: Run them and confirm they fail.**
  Run `uv run pytest -q -p no:randomly tests/test_normalize.py`.
  Expected: 3 failed.
  - `test_rule_normalize_strips_expressway_addresses`: the result is
    `'Acme Diner Oak Hollow Expy Elmton'`.
  - `test_glued_places_fold_into_the_spaced_name`: the result is `{}`.
  - `test_renormalize_folds_glued_place_descriptors`: the first call returns `0`.

  `test_one_longer_word_is_not_a_glued_place` already passes. It is the guard, and it must
  still pass after Step 3.

- [ ] **Step 3: Implement.** Make four edits to `src/moneta/pipelines/normalize.py`.

  (a) In `_ADDRESS`, replace the suffix line:

```python
    r"(?:street|st|road|rd|avenue|ave|blvd|drive|dr|lane|ln|way|pkwy|expy|expwy)\b.*$",
```

  (b) Directly after `_MAX_TAILS = 3`, add:

```python
# a feed that glues the city onto the merchant ("ACME UNIVERSITYRIVERTON CA"): a glued
# tail has at least this many letters ("Monthly" is not "Month" plus a place), and a
# multi-word name needs this many different glued tails before they read as places
# rather than one longer word ("Corner Storehouse" is not "Corner Store" plus a place)
_MIN_GLUED_LETTERS = 4
_MIN_GLUED_TAILS = 2
```

  (c) Directly above `def merge_targets`, add:

```python
def _letters(text: str) -> int:
    return sum(c.isalpha() for c in text)


def _glued(
    keyed: dict[str, tuple[str, ...]],
    sources: set[str],
    by_tokens: dict[tuple[str, ...], str],
) -> dict[str, tuple[tuple[str, ...], str]]:
    """name -> (tokens of the shorter name whose last word it extends, the glued tail):
    "Acme Universityriverton" -> (("acme", "university"), "riverton"). The longest stem
    wins; the stem and the tail both need 4+ letters."""
    stems: dict[tuple[str, ...], list[str]] = {}
    for toks in by_tokens:
        if toks and _letters(toks[-1]) >= _MIN_TARGET_LETTERS:
            stems.setdefault(toks[:-1], []).append(toks[-1])
    glued: dict[str, tuple[tuple[str, ...], str]] = {}
    for name, toks in keyed.items():
        if name not in sources or not toks:
            continue
        head, last = toks[:-1], toks[-1]
        matches = [stem for stem in stems.get(head, []) if last.startswith(stem) and last != stem]
        if not matches:
            continue
        stem = max(matches, key=len)
        tail = last[len(stem) :]
        if _letters(tail) >= _MIN_GLUED_LETTERS:
            glued[name] = ((*head, stem), tail)
    return glued
```

  (d) In `merge_targets`, make three changes.

  First, in the docstring, replace its last two lines and the closing quotes. That runs
  from `with 3+ different non-place tails absorbs only its place variants. Names in`
  through `"""`. Replace them with:

```python
    with 3+ different non-place tails absorbs only its place variants. A name whose last
    word is a shorter name's last word with a place glued on ("Acme Universityriverton")
    merges into it when the glued tail is a place, or when a multi-word name shows 2+
    different glued tails. Names in `targets_only` can absorb others but never merge
    themselves and teach no places.
    """
```

  Second, directly after the line
  `generic = {prefix for prefix, found in tails.items() if len(found) >= _MAX_TAILS}`,
  add:

```python
    glued = _glued(keyed, sources, by_tokens)
    glued_tails: dict[tuple[str, ...], set[str]] = {}
    for base, tail in glued.values():
        glued_tails.setdefault(base, set()).add(tail)

    def glued_target(name: str) -> str | None:
        """The shorter name `name` is with a place glued on, when the evidence says so."""
        if name not in glued:
            return None
        base, tail = glued[name]
        if tail in places:
            return by_tokens[base]
        if len(base) >= 2 and base not in generic and len(glued_tails[base]) >= _MIN_GLUED_TAILS:
            return by_tokens[base]
        return None
```

  Third, extend the loop's `else:` branch so it reads:

```python
        else:
            if by_tokens[toks] != name:  # same tokens, different spelling
                targets[name] = by_tokens[toks]
            elif (glued_into := glued_target(name)) is not None:
                targets[name] = glued_into
```

  The chain-resolution loop that follows is unchanged. A glued name reaches it like any
  other target.

- [ ] **Step 4: Run the tests and confirm they pass.**
  Run `uv run pytest -q -p no:randomly tests/test_normalize.py`. Expected: all pass.

- [ ] **Step 5: Update the docs.**

  `CLAUDE.md`, in the "Pipeline order is load-bearing" bullet, replace
  ``then `normalize.merge_targets` folds longer names into a shorter token-prefix name, and every rename``
  with:

```text
then `normalize.merge_targets` folds longer names into a shorter token-prefix name (and a glued place into the name it is glued onto: `Acme Universityelmton` → `Acme University`, when the tail is a learned place or a multi-word name shows 2+ different glued tails of 4+ letters), and every rename
```

  `docs/PRD.md:76` ("Merchant normalization" row) needs two replacements.
  - Replace `street addresses and city/state tails.` with
    `street addresses (expressways included) and city/state tails.`
  - Replace `so brand + product names stay apart.` with:

```text
so brand + product names stay apart. A place glued onto the last word (`Acme Universityelmton`) folds into the spaced name when the tail is a learned place, or when a multi-word name shows two or more different glued tails; one longer word beside a name (`Corner Storehouse` beside `Corner Store`) stays apart.
```

  `docs/PRD.md` §7 "Feature history": add this row directly under the `|---|---|---|`
  separator, above the newest 2026-10-14 row:

```text
| 2026-10-15 | **Bill detection across spellings** | A city glued onto a merchant name (`Acme Universityelmton`) folds into the spaced name, and expressway addresses are stripped. |
```

  `docs/user-guide.md`, in the paragraph starting "One bill is one series", needs two
  replacements. Rewrap the paragraph to its current width afterwards.
  - Replace `street addresses and city/state tails from` with
    `street addresses (expressways too) and city/state tails from`.
  - Replace ``and `Acme Fitness Riverton` become `Acme Fitness`).`` with:

```text
and `Acme Fitness Riverton` become `Acme Fitness`). A city glued onto the name folds the
same way: `Acme Universityelmton` and `Acme Universityriverton` become `Acme University`
once the tail is a known place or the name shows two different glued tails, while one
longer word (`Corner Storehouse` beside `Corner Store`) stays its own merchant.
```

- [ ] **Step 6: Run the gate.**
  Run `uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests`.
  Expected: all pass with pristine output, and ruff format leaves every file unchanged.

- [ ] **Step 7: Commit.**

```bash
git add src/moneta/pipelines/normalize.py tests/test_normalize.py CLAUDE.md docs/PRD.md docs/user-guide.md
git commit -s -m "fix: fold glued place names and strip expressway addresses (#41)"
```

---

### Task 2: Per-charge codes

**Files:**
- Modify: `src/moneta/pipelines/normalize.py` (constants after `_MIN_GLUED_TAILS`, a helper
  above `merge_targets`, and `merge_targets` itself)
- Modify: `CLAUDE.md` (the "Pipeline order is load-bearing" bullet), `docs/PRD.md:76` and §7,
  `docs/user-guide.md:738`
- Test: `tests/test_normalize.py`

**Interfaces:**
- Consumes: Task 1's `_MIN_GLUED_TAILS = 2` line, which is the insertion point. Also Task
  1's docstring sentence ending `different glued tails.`, and its CLAUDE.md and §7 text.
- Produces: the private `_charge_codes(names: set[str]) -> dict[str, str]` and the
  constants `_CHARGE_CODE` and `_MIN_CODES = 2`. The `merge_targets` signature is
  unchanged.

- [ ] **Step 1: Write the failing tests.** In `tests/test_normalize.py`, add
  `RecurringSeries,` to the `from moneta.models import (...)` block, directly after
  `MerchantAlias,`. Then append:

```python
def test_rule_normalize_keeps_a_letters_only_code_for_the_merge_pass() -> None:
    # the address strips; a letters-only code stays, since one alone may be a real name
    assert rule_normalize("ACMEAPP-QWERTYUI 12 ELM AVENUE ELMTON 99999 TX USA") == (
        "Acmeapp-Qwertyui"
    )
    # a code with a digit is a reference token and was always stripped
    assert rule_normalize("ACMEAPP-QW3RTYUI 12 ELM AVENUE ELMTON 99999 TX USA") == "Acmeapp"


def test_per_charge_codes_fold_into_their_merchant() -> None:
    assert merge_targets(["Acmeapp-Qwertyui", "Acmeapp-Asdfghjk"]) == {
        "Acmeapp-Qwertyui": "Acmeapp",
        "Acmeapp-Asdfghjk": "Acmeapp",
    }
    # beside the name a digit code's charges already carry
    assert merge_targets(["Acmeapp", "Acmeapp-Qwertyui", "Acmeapp-Asdfghjk"]) == {
        "Acmeapp-Qwertyui": "Acmeapp",
        "Acmeapp-Asdfghjk": "Acmeapp",
    }


def test_one_hyphenated_name_is_not_a_code() -> None:
    assert merge_targets(["Corner-Sandwich"]) == {}
    assert merge_targets(["Corner", "Corner-Sandwich"]) == {}
    # the same second word twice is one name, not two codes
    assert merge_targets(["Corner-Sandwich", "Corner-Sandwich Elmton"]) == {}


async def test_per_charge_codes_form_one_subscription(session: AsyncSession) -> None:
    acct = await make_account(session)
    for month, code in ((4, "QWERTYUI"), (5, "ASDFGHJK"), (6, "ZXCVBNMQ")):
        await make_txn(
            session,
            acct,
            description=f"ACMEAPP-{code} 12 ELM AVENUE ELMTON 99999 TX USA",
            amount_cents=-1500,
            posted_on=date(2026, month, 3),
        )
    await normalize_merchants(session, llm=None)

    assert await renormalize_merchants(session) == 3
    assert await renormalize_merchants(session) == 0

    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.new_series == 1
    series = (await session.execute(select(RecurringSeries))).scalar_one()
    assert series.merchant == "Acmeapp" and series.expected_cents == -1500
```

- [ ] **Step 2: Run them and confirm they fail.**
  Run `uv run pytest -q -p no:randomly tests/test_normalize.py`.
  Expected: 2 failed.
  - `test_per_charge_codes_fold_into_their_merchant`: the result is `{}`.
  - `test_per_charge_codes_form_one_subscription`: the first renormalize returns `0`.

  The rule test pins today's `rule_normalize` output, and the `Corner-Sandwich` guard
  already passes. Both must still pass after Step 3.

- [ ] **Step 3: Implement.** Make three edits to `src/moneta/pipelines/normalize.py`.

  (a) Directly after `_MIN_GLUED_TAILS = 2`, add:

```python
# a per-charge code hyphenated onto the merchant ("ACMEAPP-QWERTYUI"); a code with a digit
# is already a reference token, so only letters-only codes reach the merge pass
_CHARGE_CODE = re.compile(r"^([a-z]{4,})-([0-9a-z]{8})$", re.IGNORECASE)
# one hyphenated name may be a real one ("Corner-Sandwich"); different codes behind one
# merchant are per-charge references
_MIN_CODES = 2
```

  (b) Directly above `def merge_targets`, add:

```python
def _charge_codes(names: set[str]) -> dict[str, str]:
    """name -> the name without its per-charge code ("Acmeapp-Qwertyui" -> "Acmeapp"),
    for a merchant seen with 2+ different codes."""
    parts: dict[str, tuple[str, str]] = {}  # name -> (merchant, rest of the name)
    codes: dict[str, set[str]] = {}
    for name in names:
        first, _, rest = name.partition(" ")
        if (match := _CHARGE_CODE.match(first)) is not None:
            parts[name] = (match[1], rest)
            codes.setdefault(match[1].lower(), set()).add(match[2].lower())
    return {
        name: f"{merchant} {rest}".strip()
        for name, (merchant, rest) in parts.items()
        if len(codes[merchant.lower()]) >= _MIN_CODES
    }
```

  (c) In `merge_targets`, make two changes.

  First, replace the lines from Task 1's
  `` different glued tails. Names in `targets_only` can absorb others but never merge ``
  through `sources = set(names)` with:

```python
    different glued tails. A per-charge code ("Acmeapp-Qwertyui") is dropped once 2+
    different codes share the merchant. Names in `targets_only` can absorb others but
    never merge themselves and teach no places.
    """
    sources = set(names)
    coded = _charge_codes(sources)
    sources = (sources - coded.keys()) | set(coded.values())
```

  The coded names leave `sources` and their bare names join it, so every later guard sees
  `Acmeapp`, not each spelling. Second, directly above the
  `for name in list(targets):  # resolve chains` loop, add:

```python
    for name, bare in coded.items():  # before chains, so a code follows its merchant
        targets[name] = bare
```

- [ ] **Step 4: Run the tests and confirm they pass.**
  Run `uv run pytest -q -p no:randomly tests/test_normalize.py`. Expected: all pass.

- [ ] **Step 5: Update the docs.**

  `CLAUDE.md`, in the "Pipeline order is load-bearing" bullet, replace
  `multi-word name shows 2+ different glued tails of 4+ letters),` (Task 1's text) with:

```text
multi-word name shows 2+ different glued tails of 4+ letters; a letters-only per-charge code is dropped once 2+ different codes share a merchant: `Acmeapp-Qwertyui` → `Acmeapp`),
```

  `docs/PRD.md:76` ("Merchant normalization" row): replace
  `Bank-truncated non-prefix names and per-charge codes stay split (an LLM matching slice is deferred).`
  with:

```text
A letters-only per-charge code (`Acmeapp-Qwertyui`) is dropped once two or more different codes share the merchant; one hyphenated name alone keeps its second word. Bank-truncated non-prefix names stay split (an LLM matching slice is deferred).
```

  `docs/PRD.md` §7: replace the 2026-10-15 row Task 1 added with:

```text
| 2026-10-15 | **Bill detection across spellings** | A city glued onto a merchant name (`Acme Universityelmton`) folds into the spaced name, and expressway addresses are stripped. A letters-only per-charge code (`Acmeapp-Qwertyui`) is dropped once two or more different codes share the merchant, so a subscription billed under a new code each month forms one series. |
```

  `docs/user-guide.md:738` (troubleshooting row "One bill shows up as two series, or two
  bills as one"): replace
  ``stays split, as do descriptors with short per-charge codes.`` with:

```text
stays split, and a per-charge code (`Acmeapp-Qwertyui`) is dropped only once two different codes appear for the merchant.
```

- [ ] **Step 6: Run the gate.**
  Run `uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests`.
  Expected: all pass with pristine output, and ruff format leaves every file unchanged.

- [ ] **Step 7: Commit.**

```bash
git add src/moneta/pipelines/normalize.py tests/test_normalize.py CLAUDE.md docs/PRD.md docs/user-guide.md
git commit -s -m "fix: fold per-charge codes into their merchant (#41)"
```

---

### Task 3: Three-way auto-review of recurring questions

**Files:**
- Modify: `src/moneta/pipelines/review.py:76-80` (`_RECURRING_PROMPT`), `:94` (helpers above
  `_sample`), `:499-506` (`_validated`), `:690-692` (`verify_series` parse)
- Modify: `src/moneta/pipelines/recurring.py:310-312` (a comment only)
- Modify: `CLAUDE.md` (the "Discretionary semantics" and "LLM verification is a ledger"
  bullets), `docs/PRD.md:83` and §7, `docs/user-guide.md:601-604`
- Test: `tests/test_autoreview.py` (`:163-174` and two existing tests' scripted answers)

**Interfaces:**
- Consumes: `CLASSIFICATION_TAXONOMY` from `moneta.llm`, which `review.py` already imports.
- Produces:
  - `_CLASSIFICATIONS = ("bill", "habit", "not_recurring")`;
  - `_classification(answer: dict[str, Any] | None) -> str | None`;
  - `_cluster_resolution(label: str) -> dict[str, Any]`, which returns
    `{"is_recurring": True}`, `{"is_recurring": True, "discretionary": True}` or
    `{"is_recurring": False}`.

  The resolution shapes are unchanged, so `apply_resolution` and detection's force map
  need no edit.

- [ ] **Step 1: Write the failing tests.** In `tests/test_autoreview.py`:

  (a) Add `from moneta.views.power import power_report` directly after
  `from moneta.pipelines.run import run_sync`.

  (b) Replace the body of `test_confident_recurring_answer_feeds_next_detection` (`:163-174`)
  from its `llm = ScriptedLLM(...)` line to the end, and append the new tests after it:

```python
    llm = ScriptedLLM({"Odd Timing Co": {"classification": "bill", "confident": True}})
    assert await autoreview_items(session, llm) == 1
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.resolution == {"is_recurring": True, "resolved_by": "llm"}  # a human's "b"
    # consumes the force map
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.new_series == 1
    series = (await session.execute(select(RecurringSeries))).scalar_one()
    assert series.discretionary is False


async def test_confident_habit_answer_keeps_it_out_of_fixed_costs(session: AsyncSession) -> None:
    acct = await make_account(session)
    # steady amounts at irregular, bill-like gaps (12 and 40 days): detection asks
    for d in (date(2026, 4, 1), date(2026, 4, 13), date(2026, 5, 23)):
        await make_txn(session, acct, amount_cents=-1800, merchant="Corner Curry", posted_on=d)
    await detect_recurring(session, llm=None, today=date(2026, 6, 1))
    llm = ScriptedLLM({"Corner Curry": {"classification": "habit", "confident": True}})

    assert await autoreview_items(session, llm) == 1

    (prompt,) = llm.prompts
    assert '"habit"' in prompt and "restaurants" in prompt  # the shared three-way taxonomy
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.resolution == {  # exactly what a human's "h" writes
        "is_recurring": True,
        "discretionary": True,
        "resolved_by": "llm",
    }
    await detect_recurring(session, llm=None, today=date(2026, 6, 1))
    series = (await session.execute(select(RecurringSeries))).scalar_one()
    assert series.discretionary is True and series.status == SeriesStatus.active
    report = await power_report(session, today=date(2026, 6, 1))
    assert report.fixed_costs == [] and report.total_fixed_cents == 0


async def test_confident_not_recurring_answer_suppresses_the_group(session: AsyncSession) -> None:
    acct = await make_account(session)
    for d in (date(2026, 4, 1), date(2026, 4, 11), date(2026, 5, 26)):
        await make_txn(session, acct, amount_cents=-30000, merchant="Odd Timing Co", posted_on=d)
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    llm = ScriptedLLM({"Odd Timing Co": {"classification": "not_recurring", "confident": True}})

    assert await autoreview_items(session, llm) == 1

    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.resolution == {"is_recurring": False, "resolved_by": "llm"}  # a human's "n"
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.new_series == 0 and stats.review == 0


@pytest.mark.parametrize(
    "answer",
    [
        {"classification": "habit", "confident": False},
        {"classification": "subscription", "confident": True},
        {"is_recurring": True, "confident": True},  # the old yes/no shape
    ],
)
async def test_unsure_or_unknown_classification_stays_open(
    session: AsyncSession, answer: dict[str, Any]
) -> None:
    acct = await make_account(session)
    for d in (date(2026, 4, 1), date(2026, 4, 11), date(2026, 5, 26)):
        await make_txn(session, acct, amount_cents=-30000, merchant="Odd Timing Co", posted_on=d)
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))

    assert await autoreview_items(session, ScriptedLLM({"Odd Timing Co": answer})) == 0

    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.status == ReviewStatus.open and item.resolution is None
    assert "llm_flagged" not in item.payload  # still askable next sync
```

  (c) Two existing tests script the old answer shape. They still pass on it, but only
  because nothing parses it now. Move them to the new shape so they keep testing the
  skip rule:
  - In `test_llm_declined_detection_item_stays_human_only`, change the scripted answer
    `{"is_recurring": True, "confident": True}` to
    `{"classification": "bill", "confident": True}` and leave the merchant key as is.
    `ruff format` wraps the call onto three lines. In the same test's docstring, replace
    `so autoreview's binary is_recurring\n    prompt can never` with
    `so autoreview's recurring prompt can\n    never`. Replace the comment
    `# next sync: autoreview must not even ask its old binary bill/one-off question` with
    `# next sync: autoreview must not ask the LLM a second time`.
  - In `test_autoreview_skips_llm_flagged_items` (`:336`), make the same change to the
    scripted answer, with the merchant key unchanged.

- [ ] **Step 2: Run them and confirm they fail.**
  Run `uv run pytest -q -p no:randomly tests/test_autoreview.py`.
  Expected: 4 failed.
  - `test_confident_recurring_answer_feeds_next_detection`: `0 == 1`, since today's
    `_validated` needs `is_recurring`.
  - `test_confident_habit_answer_keeps_it_out_of_fixed_costs`: `0 == 1`.
  - `test_confident_not_recurring_answer_suppresses_the_group`: `0 == 1`.
  - `test_unsure_or_unknown_classification_stays_open[answer2]`: `1 == 0`, since the old
    shape still resolves.

  The other two parametrized cases pass already.

- [ ] **Step 3: Implement.**

  (a) In `src/moneta/pipelines/review.py`, replace `_RECURRING_PROMPT` with:

```python
_RECURRING_PROMPT = (
    "You are auto-resolving a personal-finance review question.\n"
    "Classify this merchant's transactions.\n"
    f"{CLASSIFICATION_TAXONOMY}\n"
    "Merchant: {merchant!r}; direction: {direction}; recent charges: {samples}\n"
    'Respond with JSON: {{"classification": "bill" | "habit" | "not_recurring", '
    '"confident": true/false}}\n'
    "Set confident=true ONLY if the pattern is clear."
)
```

  The f-string fills the taxonomy at import time. Its doubled braces stay literal for the
  later `.format(merchant=..., direction=..., samples=...)` call in `autoreview_items`,
  which is unchanged.

  (b) Directly above `def _sample(txn: Transaction)`, add:

```python
_CLASSIFICATIONS = ("bill", "habit", "not_recurring")


def _classification(answer: dict[str, Any] | None) -> str | None:
    """A three-way recurring answer's label; None when it is missing or malformed."""
    label = answer.get("classification") if answer else None
    return label if label in _CLASSIFICATIONS else None


def _cluster_resolution(label: str) -> dict[str, Any]:
    """The resolution `moneta review`'s b/h/n answer writes for one label."""
    if label == "habit":
        return {"is_recurring": True, "discretionary": True}
    return {"is_recurring": label == "bill"}
```

  (c) In `_validated`, the `recurring_cluster` branch becomes:

```python
    if item.kind == ReviewKind.recurring_cluster:
        label = _classification(answer)
        return None if label is None else _cluster_resolution(label)
    return None
```

  `_validated` still returns `None` first when `confident` is not `True`, so an
  unconfident answer never reaches this branch.

  (d) In `verify_series`, replace these three lines:

```python
        classification = answer.get("classification") if answer else None
        if classification not in ("bill", "habit", "not_recurring"):
            classification = None
```

  with:

```python
        classification = _classification(answer)
```

  (e) In `src/moneta/pipelines/recurring.py`, in the unstable-amount branch of
  `_detect_bill`, replace this comment (keep the line above and the line below it):

```python
            # mirror verify_series: human-only from here, autoreview must not re-ask
            # the binary bill/one-off prompt and rubber-stamp it as a fixed cost.
```

  with:

```python
            # mirror verify_series: human-only from here, autoreview must not ask the
            # LLM again and let a second answer overrule the first.
```

- [ ] **Step 4: Run the tests and confirm they pass.**
  Run `uv run pytest -q -p no:randomly tests/test_autoreview.py`. Expected: all pass.

- [ ] **Step 5: Update the docs.**

  `CLAUDE.md`, "Discretionary semantics" bullet: replace
  ``set by an amount-unstable group's LLM "habit" answer, or a human's``
  with:

```text
set by an amount-unstable group's LLM "habit" answer, auto-review's confident "habit" answer to an open `recurring_cluster` question (`review._cluster_resolution` maps bill/habit/not_recurring to exactly what b/h/n write), or a human's
```

  `CLAUDE.md`, "LLM verification is a ledger, not a veto" bullet: replace
  ``Verification is three-way (`bill`/`habit`/`not_recurring`, same as detection's own classification):``
  with:

```text
Verification is three-way (`bill`/`habit`/`not_recurring`, same as detection's own classification and auto-review's; `review._classification` parses it for both review paths):
```

  `docs/PRD.md:83` ("LLM auto-review" row): replace the whole second cell
  `Confident LLM answers resolve open review items during sync (before detection, so answers shape the same run). Unconfident/malformed answers fall through to the human queue.`
  with:

```text
Confident LLM answers resolve open review items during sync (before detection, so answers shape the same run). A recurring question is classified three ways with the same taxonomy as detection and verification: a confident bill, habit or not-recurring answer resolves it exactly as `moneta review`'s `b`, `h` or `n` would, so a habit becomes a discretionary series kept out of fixed costs. Unconfident/malformed answers fall through to the human queue. Questions the LLM resolved under the earlier yes/no prompt are not re-asked.
```

  `docs/PRD.md` §7: replace the 2026-10-15 row with:

```text
| 2026-10-15 | **Bill detection across spellings** | A city glued onto a merchant name (`Acme Universityelmton`) folds into the spaced name, and expressway addresses are stripped. A letters-only per-charge code (`Acmeapp-Qwertyui`) is dropped once two or more different codes share the merchant, so a subscription billed under a new code each month forms one series. Auto-review classifies recurring questions bill / habit / not recurring like detection and verification, and a confident habit answer resolves the question as a human's `h` does, keeping the merchant out of fixed costs. |
```

  `docs/user-guide.md:601-604` (the paragraph starting "With an LLM configured"): directly
  after its first sentence, which ends `comes to you.`, insert the text below. Then rewrap
  the paragraph to its current width.

```text
A recurring question gets the same bill / habit / not-recurring choice you have, and a
confident habit answer is recorded exactly like your `h`: the merchant is tracked as
discretionary spending, not a fixed cost.
```

- [ ] **Step 6: Run the gate.**
  Run `uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests`.
  Expected: all pass with pristine output, and ruff format leaves every file unchanged.

- [ ] **Step 7: Commit.**

```bash
git add src/moneta/pipelines/review.py src/moneta/pipelines/recurring.py tests/test_autoreview.py CLAUDE.md docs/PRD.md docs/user-guide.md
git commit -s -m "fix: auto-review classifies recurring questions bill/habit/not recurring (#41)"
```

---

### Task 4: A term deposit beside a monthly installment is spending, not the bill

**Files:**
- Modify: `src/moneta/pipelines/bills.py:12` (the `datetime` import) and `:99-132`
  (`_scheduled_units`)
- Modify: `CLAUDE.md` (the "A merchant can have several series" bullet), `docs/PRD.md:79`
  and §7, `docs/user-guide.md` (the paragraph starting "One bill is one series")
- Test: `tests/test_bills.py`, `tests/test_recurring.py`

**Interfaces:**
- Consumes: Task 1's glued-place fold, through the end-to-end test. Its three spellings
  must reach detection as one merchant.
- Produces: no new names. `split_bills`, `whole_bill` and `_scheduled_units` keep their
  signatures; `_scheduled_units` only widens the window in which it looks for extras.

  `recurring.py` needs no change. A group that splits already becomes one bill per
  scheduled unit, each a new series at `_typical_amount`, and a charge outside every unit
  is never tagged. The deposits match no cadence, so they form no unit and open no
  question.

- [ ] **Step 1: Write the failing tests.** Append to `tests/test_bills.py`. `_t` and
  `_monthly` are the module's existing helpers.

```python
def test_a_charge_after_the_schedule_before_it_is_due_splits_it() -> None:
    # three installments, then a deposit before the next installment is due
    installments = _monthly(15, range(6, 9), -120000)
    assert split_bills([*installments, _t(date(2026, 9, 1), -210000)]) == [installments]


def test_a_repriced_charge_in_the_due_window_does_not_split() -> None:
    # the next charge at a new price is that charge, not an extra: one bill, as before
    charges = [*_monthly(15, range(5, 8), -1500), _t(date(2026, 8, 12), -2000)]
    assert split_bills(charges) is None


def test_a_later_charge_closes_the_not_yet_due_window() -> None:
    # a purchase just after the newest charge, then the plan goes on at a new price: the
    # next charge came due, so the purchase is judged by the run alone, as before
    charges = [
        *_monthly(9, range(3, 7), -4000),
        _t(date(2026, 6, 12), -30000),
        *_monthly(9, range(7, 10), -8000),
    ]
    assert split_bills(charges) is None
```

  In `tests/test_recurring.py`, replace the import lines

```python
from moneta.pipelines.events import emit_series_events
from moneta.pipelines.recurring import detect_recurring
```

  with:

```python
from moneta.pipelines.events import emit_series_events
from moneta.pipelines.normalize import normalize_merchants, renormalize_merchants
from moneta.pipelines.recurring import detect_recurring
from moneta.views.power import power_report
```

  Then append the tests below. `Account`, `ReviewStatus`, `recurring_cluster_item` and
  `make_series` are already imported.

```python
# a payment plan charged from two cards under three spellings: a monthly installment
# (December and May paid from elsewhere), a larger deposit at each term start, and a fee
# beside the newest deposit
_INSTALLMENTS = (
    date(2025, 10, 15),
    date(2025, 11, 14),
    date(2026, 1, 15),
    date(2026, 2, 16),
    date(2026, 3, 16),
    date(2026, 4, 15),
    date(2026, 6, 15),
    date(2026, 7, 15),
    date(2026, 8, 14),
)
_DEPOSITS = (date(2025, 9, 2), date(2026, 1, 5), date(2026, 5, 4), date(2026, 9, 1))


async def _payment_plan(session: AsyncSession) -> list[Account]:
    cards = [await make_account(session, type=AccountType.credit) for _ in range(2)]
    for i, posted_on in enumerate(_INSTALLMENTS):
        raw = ("ACME UNIVERSITY CA", "ACME UNIVERSITYELMTON CA")[i % 2]
        await make_txn(
            session, cards[i % 2], amount_cents=-120000, description=raw, posted_on=posted_on
        )
    for cents, posted_on in [(-210000, d) for d in _DEPOSITS] + [(-14000, _DEPOSITS[-1])]:
        await make_txn(
            session,
            cards[0],
            amount_cents=cents,
            description="ACME UNIVERSITYRIVERTON CA",
            posted_on=posted_on,
        )
    await normalize_merchants(session, llm=None)
    await renormalize_merchants(session)
    return cards


async def test_term_deposits_stay_out_of_a_monthly_payment_plan(session: AsyncSession) -> None:
    """The deposit after the newest installment, before the next one is due, sits beside
    the schedule: the installment is the bill, and every deposit is ordinary spending."""
    await _payment_plan(session)
    today = date(2026, 9, 10)

    stats = await detect_recurring(session, llm=None, today=today)

    assert stats.new_series == 1 and stats.review == 0
    series = (await _series(session))[0]
    assert (series.merchant, series.cadence, series.expected_cents) == (
        "Acme University",
        Cadence.monthly,
        -120000,
    )
    assert series.status == SeriesStatus.active and series.discretionary is False
    txns = (await session.execute(select(Transaction))).scalars().all()
    assert sorted(t.posted_on for t in txns if t.series_id == series.id) == list(_INSTALLMENTS)
    report = await power_report(session, today=today)
    assert [(line.merchant, line.monthly_cents) for line in report.fixed_costs] == [
        ("Acme University", 120000)
    ]
    assert report.spent_so_far_cents == 224000  # the newest deposit and its fee

    stats = await detect_recurring(session, llm=None, today=today)

    assert (stats.new_series, stats.updated, stats.review) == (0, 0, 0)


async def test_a_merchant_level_bill_ruling_never_tags_the_deposits(session: AsyncSession) -> None:
    """A "bill" answer to the merchant-level question covers every bill the merchant has,
    and the deposits are none of them: charges an earlier ruling tagged leave the series."""
    await _payment_plan(session)
    series = await make_series(
        session,
        merchant="Acme University",
        expected_cents=-120000,
        next_expected_on=date(2026, 9, 14),
    )
    for t in (await session.execute(select(Transaction))).scalars():
        t.series_id = series.id  # what the ruling tagged while the merchant was one bill
    ruling = recurring_cluster_item("Acme University", "outflow")
    ruling.status = ReviewStatus.resolved
    ruling.resolution = {"is_recurring": True, "resolved_by": "manual"}
    session.add(ruling)
    await session.commit()

    stats = await detect_recurring(session, llm=None, today=date(2026, 9, 10))

    assert (stats.new_series, stats.review) == (0, 0)
    assert [s.id for s in await _series(session)] == [series.id]
    assert series.expected_cents == -120000 and series.discretionary is False
    txns = (await session.execute(select(Transaction))).scalars().all()
    assert sorted(t.posted_on for t in txns if t.series_id == series.id) == list(_INSTALLMENTS)
```

- [ ] **Step 2: Run them and confirm they fail.**
  Run `uv run pytest -q -p no:randomly tests/test_bills.py tests/test_recurring.py`.
  Expected: 3 failed.
  - `test_a_charge_after_the_schedule_before_it_is_due_splits_it`: `split_bills` returns
    `None`.
  - `test_term_deposits_stay_out_of_a_monthly_payment_plan`: `stats` is
    `RecurringStats(new_series=0, updated=0, review=1, merged=0)`, one merchant-level
    question and no series.
  - `test_a_merchant_level_bill_ruling_never_tags_the_deposits`: the tagged dates include
    the deposits and the fee.

  `test_a_repriced_charge_in_the_due_window_does_not_split` and
  `test_a_later_charge_closes_the_not_yet_due_window` already pass. They are the guards,
  and they must still pass after Step 3, as must the existing
  `test_variable_bill_with_a_small_fee_is_not_split`.

- [ ] **Step 3: Implement.** Make three edits to `src/moneta/pipelines/bills.py`.

  (a) Replace `from datetime import date` with:

```python
from datetime import date, timedelta
```

  (b) In the `_scheduled_units` docstring, replace
  `real size (not an adjustment) sits inside a unit's schedule without belonging to it.`
  with:

```python
    real size (not an adjustment) sits inside a unit's schedule without belonging to it:
    within its newest run, or after it while the next charge is not due yet.
```

  (c) Replace the line `start, end = match[1], dates[-1]` with:

```python
        (cadence, start), end = match, dates[-1]
        # nothing charged since the next charge came due: a later charge (a term deposit
        # after the newest installment) sits alongside the schedule too
        due = advance_expected_on(end, cadence) - timedelta(days=TOLERANCE[cadence])
        if all(t.posted_on < due for t in group):
            end = max(t.posted_on for t in group)
```

  The extras test below it is unchanged: a non-member of at least `MINOR_FRACTION` of the
  unit's median, dated within `[start, end]`. Subtracting `TOLERANCE` keeps a repriced
  charge that arrives early from counting as an extra. The `all(...)` guard keeps the
  window shut once the group has charged at or after the due window, so history that
  already went on is judged exactly as before.

- [ ] **Step 4: Run the tests and confirm they pass.**
  Run `uv run pytest -q -p no:randomly tests/test_bills.py tests/test_recurring.py`.
  Expected: all pass.

- [ ] **Step 5: Update the docs.**

  `CLAUDE.md`, "A merchant can have several series" bullet: replace
  `only when a schedule has extra charges alongside it, or two same-price schedules overlap in time`
  with:

```text
only when a schedule has extra charges alongside it (inside its newest run, or after its newest charge while every charge is still before the next due date minus `cadence.TOLERANCE`), or two same-price schedules overlap in time
```

  `docs/PRD.md:79` ("Recurring detection" row): replace
  `a group splits into bills only when a schedule has extra charges alongside it or two same-price schedules overlap;`
  with:

```text
a group splits into bills only when a schedule has extra charges alongside it (inside its newest run, or after its newest charge before the next one is due, such as a term deposit beside a monthly installment) or two same-price schedules overlap;
```

  `docs/PRD.md` §7: replace the 2026-10-15 row with:

```text
| 2026-10-15 | **Bill detection across spellings** | A city glued onto a merchant name (`Acme Universityelmton`) folds into the spaced name, and expressway addresses are stripped. A letters-only per-charge code (`Acmeapp-Qwertyui`) is dropped once two or more different codes share the merchant, so a subscription billed under a new code each month forms one series. Auto-review classifies recurring questions bill / habit / not recurring like detection and verification, and a confident habit answer resolves the question as a human's `h` does, keeping the merchant out of fixed costs. A larger charge after a monthly schedule's newest charge, before the next one is due, splits the merchant's bills, so a term deposit beside a payment-plan installment is ordinary spending and the installment stays a fixed cost at its own amount. |
```

  `docs/user-guide.md`, in the paragraph starting "One bill is one series" (Task 1
  rewrapped it): directly after `series and the other charges are ordinary spending.`,
  insert the text below. Then rewrap the paragraph to its current width.

```text
A charge that lands after a schedule's newest charge, before the next one is due, counts
the same way: a term deposit beside a monthly payment-plan installment is ordinary
spending, and the installment alone is the fixed cost.
```

- [ ] **Step 6: Run the gate.**
  Run `uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests`.
  Expected: all pass with pristine output, and ruff format leaves every file unchanged.

- [ ] **Step 7: Commit.**

```bash
git add src/moneta/pipelines/bills.py tests/test_bills.py tests/test_recurring.py CLAUDE.md docs/PRD.md docs/user-guide.md
git commit -s -m "fix: keep a term deposit out of a monthly installment's bill (#41)"
```

---

### Task 5: A payment plan holds across one missed month

**Files:**
- Modify: `src/moneta/cadence.py` (a function directly after `match_cadence`)
- Modify: `src/moneta/pipelines/bills.py` (the `moneta.cadence` import, a helper above
  `_scheduled_units`, its docstring and its `match_cadence` line)
- Modify: `src/moneta/pipelines/recurring.py` (the `moneta.cadence` import, `newest_run`,
  and one line of `_detect_bill`)
- Modify: `CLAUDE.md` (the "A merchant can have several series" and "Events semantics"
  bullets), `docs/PRD.md:79` and §7, `docs/user-guide.md` (the text Task 4 added)
- Test: `tests/test_bills.py`, `tests/test_recurring.py`

**Interfaces:**
- Consumes: Task 4's not-yet-due window in `_scheduled_units`, its `_payment_plan` helper
  (it returns the two cards) and `_INSTALLMENTS`, and its docs text.
- Produces:
  - `cadence.match_across_a_miss(dates: list[date]) -> tuple[Cadence, date, date] | None`,
    returning the cadence, the run's start and the charge before the missed month;
  - the private `bills._across_a_miss(dates: list[date], group: Sequence[Transaction]) -> tuple[Cadence, date] | None`;
  - `newest_run(charges, *, across_a_miss: bool = False)`. The default keeps events.py's
    and review.py's calls unchanged; only `_detect_bill` passes it, for a split bill.

  Only a unit `_across_a_miss` accepted can reach `_detect_bill` as a split bill without a
  strict cadence, so detection may cross the miss without repeating the guards.

- [ ] **Step 1: Write the failing tests.** In `tests/test_bills.py`, replace
  `from moneta.models import Transaction` with:

```python
from moneta.cadence import match_across_a_miss
from moneta.models import Cadence, Transaction
```

  Then append:

```python
def test_match_across_a_miss_bridges_exactly_one_missed_month() -> None:
    dates = [date(2026, 6, 15), date(2026, 7, 15), date(2026, 8, 15), date(2026, 10, 15)]
    assert match_across_a_miss(dates) == (Cadence.monthly, date(2026, 6, 15), date(2026, 8, 15))
    # two months in a row, or two separate months, are a pause, not one missed month
    assert match_across_a_miss([date(2026, 6, 15), date(2026, 7, 15), date(2026, 10, 15)]) is None
    assert match_across_a_miss([date(2026, 4, 15), date(2026, 6, 15), date(2026, 8, 15)]) is None


def test_a_schedule_holds_across_one_missed_month() -> None:
    # September's installment was paid from elsewhere: only the deposit posted that month
    installments = [*_monthly(15, range(6, 9), -120000), _t(date(2026, 10, 15), -120000)]
    assert split_bills([*installments, _t(date(2026, 9, 1), -210000)]) == [installments]


def test_a_charge_on_the_missed_due_date_is_that_month_not_a_miss() -> None:
    # a variable bill whose July charge fell into another amount band: July was billed
    charges = [
        _t(date(2026, 6, 30), -9000),
        _t(date(2026, 7, 31), -6000),
        _t(date(2026, 8, 31), -8500),
        _t(date(2026, 9, 30), -10500),
    ]
    assert split_bills(charges) is None


def test_a_habit_that_repeats_a_price_is_no_schedule_across_a_miss() -> None:
    # the same dish some days or weeks apart, nothing at that price in August
    dish = [(5, 24), (6, 8), (6, 12), (6, 15), (7, 13), (9, 21)]
    charges = [
        *(_t(date(2026, month, day), -4000) for month, day in dish),
        _t(date(2026, 8, 1), -5500),
        _t(date(2026, 8, 23), -10000),
    ]
    assert split_bills(charges) is None
```

  Append to `tests/test_recurring.py` (`EventKind` and `SeriesEvent` are already imported):

```python
async def test_a_payment_plan_holds_across_a_month_with_only_a_deposit(
    session: AsyncSession,
) -> None:
    """September's installment was paid from an account that isn't linked, so only the
    deposit posted that month: October's installment continues the plan."""
    cards = await _payment_plan(session)
    await detect_recurring(session, llm=None, today=date(2026, 9, 10))
    await make_txn(
        session,
        cards[1],
        amount_cents=-120000,
        description="ACME UNIVERSITY CA",
        posted_on=date(2026, 10, 15),
    )
    await normalize_merchants(session, llm=None)
    await renormalize_merchants(session)

    stats = await detect_recurring(session, llm=None, today=date(2026, 10, 20))

    assert (stats.new_series, stats.review) == (0, 0)
    (series,) = await _series(session)
    assert (series.cadence, series.expected_cents, series.status) == (
        Cadence.monthly,
        -120000,
        SeriesStatus.active,
    )
    txns = (await session.execute(select(Transaction))).scalars().all()
    assert sorted(t.posted_on for t in txns if t.series_id == series.id) == [
        *_INSTALLMENTS,
        date(2026, 10, 15),
    ]
    items = (await session.execute(select(ReviewItem))).scalars().all()
    assert [i for i in items if i.status == ReviewStatus.open] == []
    events = (await session.execute(select(SeriesEvent))).scalars().all()
    # nothing posted around September's due date: one missed event, as for any bill
    assert [e.occurred_on for e in events if e.kind == EventKind.missed] == [date(2026, 9, 14)]
```

- [ ] **Step 2: Run them and confirm they fail.**
  - Run `uv run pytest -q -p no:randomly tests/test_bills.py`. Expected: a collection error,
    `ImportError: cannot import name 'match_across_a_miss' from 'moneta.cadence'`.
  - Run `uv run pytest -q -p no:randomly tests/test_recurring.py`. Expected: 1 failed,
    `test_a_payment_plan_holds_across_a_month_with_only_a_deposit`. Its tagged dates lack
    2026-10-15: nothing splits, so October's installment stays untagged.

  Once the import resolves, `test_a_charge_on_the_missed_due_date_is_that_month_not_a_miss`
  and `test_a_habit_that_repeats_a_price_is_no_schedule_across_a_miss` pass. They are the
  guards (Review Focus 6): each fails if its check in `_across_a_miss` is removed.

- [ ] **Step 3: Implement.**

  (a) In `src/moneta/cadence.py`, directly after `match_cadence`, add:

```python
def match_across_a_miss(dates: list[date]) -> tuple[Cadence, date, date] | None:
    """A monthly run that holds only across one missed month: the cadence, the run's start
    and the date of the charge before the miss. None when no such run reaches 3 dates.

    `match_cadence` ends a run at the first gap it cannot place, so one month paid from
    elsewhere cuts a schedule down to the charges after it.
    """
    days, tol = CADENCE_DAYS[Cadence.monthly], TOLERANCE[Cadence.monthly]
    gaps = [(b - a).days for a, b in zip(dates, dates[1:], strict=False)]
    start, missed = len(dates) - 1, None
    while start > 0:
        gap = gaps[start - 1]
        if missed is None and abs(gap - 2 * days) <= tol * 2:
            missed = start - 1
        elif abs(gap - days) > tol * 2:
            break
        start -= 1
    if missed is None or len(dates) - start < _MIN_OCCURRENCES:
        return None
    periods = [gap / 2 if i == missed else gap for i, gap in enumerate(gaps) if i >= start]
    if abs(statistics.median(periods) - days) > tol:
        return None
    return Cadence.monthly, dates[start], dates[missed]
```

  `_MIN_OCCURRENCES`, `CADENCE_DAYS`, `TOLERANCE` and `statistics` are already in the
  module. The two-period band (48–72 days) and the one-period band (18–42 days) do not
  overlap.

  (b) In `src/moneta/pipelines/bills.py`, replace
  `from moneta.cadence import TOLERANCE, advance_expected_on, match_cadence` with:

```python
from moneta.cadence import (
    CADENCE_DAYS,
    TOLERANCE,
    advance_expected_on,
    match_across_a_miss,
    match_cadence,
)
```

  Directly above `def _scheduled_units`, add:

```python
def _across_a_miss(dates: list[date], group: Sequence[Transaction]) -> tuple[Cadence, date] | None:
    """A unit's monthly schedule across one missed month (an installment paid from an
    account that isn't linked). Not when the merchant charged anything around the missed
    due date: that is the month's bill at another size. Not when the unit charges twice
    within a month: a habit that repeats a price is not a schedule."""
    bridged = match_across_a_miss(dates)
    if bridged is None:
        return None
    cadence, start, before = bridged
    missed_on = advance_expected_on(before, cadence)
    if any(abs((t.posted_on - missed_on).days) <= TOLERANCE[cadence] for t in group):
        return None
    shortest = CADENCE_DAYS[cadence] - 2 * TOLERANCE[cadence]
    if any((b - a).days < shortest for a, b in itertools.pairwise(dates)):
        return None
    return cadence, start
```

  In the `_scheduled_units` docstring, replace
  `within its newest run, or after it while the next charge is not due yet.` with:

```python
    within its newest run, or after it while the next charge is not due yet. A run holds
    across one missed month (`_across_a_miss`).
```

  In its loop, replace `match = match_cadence(dates)` with:

```python
        match = match_cadence(dates) or _across_a_miss(dates, group)
```

  The rest of the loop is unchanged. A unit accepted across a miss is a scheduled unit
  like any other, so `whole_bill` also measures "small" against it.

  (c) In `src/moneta/pipelines/recurring.py`, replace the one-line `moneta.cadence` import
  with:

```python
from moneta.cadence import (
    CADENCE_DAYS,
    GRACE_DAYS,
    TOLERANCE,
    advance_expected_on,
    match_across_a_miss,
    match_cadence,
)
```

  Replace `newest_run`'s signature, docstring and first two body lines, down to and
  including `match = match_cadence(sorted({t.posted_on for t in ordered}))`, with:

```python
def newest_run(
    charges: Sequence[Transaction], *, across_a_miss: bool = False
) -> tuple[Cadence | None, list[Transaction]]:
    """A bill's cadence and its newest cadence run, oldest first — every charge when no
    cadence holds. Shared with events.py, so both stages judge a bill on the same charges.
    `across_a_miss` lets the run continue over one missed month, as a split bill's does
    (bills.py checked that the month really was missed)."""
    ordered = sorted(charges, key=lambda t: (t.posted_on, t.id))
    dates = sorted({t.posted_on for t in ordered})
    match = match_cadence(dates)
    if match is None and across_a_miss:
        bridged = match_across_a_miss(dates)
        match = None if bridged is None else bridged[:2]
```

  In `_detect_bill`, replace `cadence, recent = newest_run(significant)` with:

```python
    cadence, recent = newest_run(significant, across_a_miss=split)
```

  Without this line a split bill crossing a miss has no cadence and opens an
  amount-specific question.

- [ ] **Step 4: Run the tests and confirm they pass.**
  Run `uv run pytest -q -p no:randomly tests/test_bills.py tests/test_recurring.py`.
  Expected: all pass.

- [ ] **Step 5: Update the docs.**

  `CLAUDE.md`, "A merchant can have several series" bullet: replace Task 4's text
  ``while every charge is still before the next due date minus `cadence.TOLERANCE`),``
  with:

```text
while every charge is still before the next due date minus `cadence.TOLERANCE`; a monthly run also holds across one missed month, `bills._across_a_miss`, when nothing was charged within `TOLERANCE` of the missed due date and the unit never charges twice within 18 days),
```

  `CLAUDE.md`, "Events semantics" bullet: replace
  `` `newest_run` is shared so detection and events judge a bill on the same charges.`` with:

```text
`newest_run` is shared so detection and events judge a bill on the same charges; only detection passes `across_a_miss=True`, for a split bill whose run `bills._across_a_miss` already accepted across one missed month.
```

  `docs/PRD.md:79` ("Recurring detection" row): replace Task 4's text
  `such as a term deposit beside a monthly installment)` with:

```text
such as a term deposit beside a monthly installment; a monthly schedule holds across one missed month when nothing else was charged around its due date)
```

  `docs/PRD.md` §7: in the 2026-10-15 row, replace
  `is ordinary spending and the installment stays a fixed cost at its own amount. |` with:

```text
is ordinary spending and the installment stays a fixed cost at its own amount. One installment paid from elsewhere no longer breaks the plan: a monthly schedule holds across a single missed month when nothing was charged around its due date. |
```

  `docs/user-guide.md`: directly after Task 4's sentence ending
  `and the installment alone is the fixed cost.`, insert the text below. Then rewrap the
  paragraph to its current width.

```text
One month paid from an account Moneta can't see does not break the plan: the next
installment continues it, and that month shows as a missed payment.
```

- [ ] **Step 6: Run the gate.**
  Run `uv run pytest -q && uv run ruff check . && uv run ruff format . && uv run mypy --strict src tests`.
  Expected: all pass with pristine output, and ruff format leaves every file unchanged.

- [ ] **Step 7: Commit.**

```bash
git add src/moneta/cadence.py src/moneta/pipelines/bills.py src/moneta/pipelines/recurring.py tests/test_bills.py tests/test_recurring.py CLAUDE.md docs/PRD.md docs/user-guide.md
git commit -s -m "fix: a payment plan holds across one missed month (#41)"
```
