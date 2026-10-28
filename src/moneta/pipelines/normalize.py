import re
import statistics
from collections.abc import Iterable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.llm import Classifier
from moneta.models import (
    AliasSource,
    MerchantAlias,
    RecurringSeries,
    ReviewItem,
    ReviewKind,
    Transaction,
    bill_ref,
)

_PREFIXES = re.compile(
    r"^(aplpay\b|apple pay\b|gpay\b|google pay\b|sq ?\*|tst ?\*|paypal ?\*|py ?\*|dd ?\*|sp ?\*"
    r"|amzn mktp us\*)\s*",
    re.IGNORECASE,
)
# ACH metadata ("WEB ID: 123…", "PPD ID: …") is not part of the merchant
_ACH_TAIL = re.compile(r"\b(?:web|ppd|ccd|tel)\s+id\b:?.*$", re.IGNORECASE)
# a word of a street name: hyphens and apostrophes inside ("Nord-Est"), an initial or
# abbreviation with its period ("E.", "Jr."), or an ordinal ("3rd")
_STREET_WORD = r"(?:[a-z]+(?:[-'][a-z]+)*\.?|\d+(?:st|nd|rd|th))"
# a street address and everything after it; runs before _STORE_NUM eats the number. The
# house number may be glued onto the last, truncated word of a fixed-width name field
# ("SUBSCRIPTION12 ELM ST"): that word goes too, as _REF_TOKEN drops a long one, unless
# it starts the name or follows a processor's star ("ZAPPROC* ACMESOUND12 …" names the
# merchant the processor bills for). A numbered building may stand before the address
# ("7 ACME TRADE CENTER 100 ELM STREET") when a word precedes it, so a name starting
# with a number stays. A store number's "#" goes with it ("#1234 ST …"), never left
# behind; "CT" is not a court type, since a trailing "CT" is the state
_ADDRESS = re.compile(
    rf"(?:(?<=\S\s)\d+\s+(?:{_STREET_WORD}\s+){{0,3}}?(?:center|plaza|tower|building)\s+)?"
    rf"(?:(?<=[^\s*]\s)[a-z]+)?(?<![\d#])#?\d+(?:st|nd|rd|th)?\s+(?:{_STREET_WORD}\s+){{0,4}}?"
    r"(?:street|st|road|rd|avenue|ave|boulevard|blvd|drive|dr|lane|ln|way|parkway|pkwy"
    r"|expy|expwy|court|highway|hwy|suite|ste)\b.*$",
    re.IGNORECASE,
)
_LEGAL = re.compile(r"\b(?:llc|inc|corp|ltd)\b\.?,?", re.IGNORECASE)
_STORE_NUM = re.compile(r"(#\d+|(?<![\w-])\d{3,}(?![\w-]))")
# payment-reference tokens: 8+ alphanumeric chars containing at least one digit
_REF_TOKEN = re.compile(r"\b(?=[0-9a-zA-Z]*\d)[0-9a-zA-Z]{8,}\b")
_SPACES = re.compile(r"\s+")
_US_STATES = frozenset(
    [
        "al",
        "ak",
        "az",
        "ar",
        "ca",
        "co",
        "ct",
        "de",
        "fl",
        "ga",
        "hi",
        "id",
        "il",
        "in",
        "ia",
        "ks",
        "ky",
        "la",
        "me",
        "md",
        "ma",
        "mi",
        "mn",
        "ms",
        "mo",
        "mt",
        "ne",
        "nv",
        "nh",
        "nj",
        "nm",
        "ny",
        "nc",
        "nd",
        "oh",
        "ok",
        "or",
        "pa",
        "ri",
        "sc",
        "sd",
        "tn",
        "tx",
        "ut",
        "vt",
        "va",
        "wa",
        "wv",
        "wi",
        "wy",
        "dc",
        "pr",
    ]
)


def rule_normalize(descriptor: str) -> str:
    """Strip what banks wrap around a merchant name (design 2026-10-13 §1)."""
    s = descriptor.strip()
    while (stripped := _PREFIXES.sub("", s)) != s:  # "AplPay TST* Name" nests prefixes
        s = stripped
    s = _ACH_TAIL.sub("", s)
    s = _ADDRESS.sub("", s)
    s = _REF_TOKEN.sub("", s)
    s = _STORE_NUM.sub("", s)
    s = _LEGAL.sub("", s).replace("*", " ")
    tokens = _SPACES.sub(" ", s).strip(" -:").split(" ")
    # location tails, but never the last remaining token
    while len(tokens) > 1 and tokens[-1].lower() in ("usa", "us"):
        tokens.pop()
    if len(tokens) > 1 and tokens[-1].lower() in _US_STATES:
        tokens.pop()
    if len(tokens) > 1 and tokens[-1].lower() == "co":
        tokens.pop()
    s = " ".join(tokens).strip(" -:")
    return s.title() if s else descriptor.strip().title()


def looks_clean(merchant: str) -> bool:
    if not merchant:
        return False
    letters = sum(c.isalpha() for c in merchant)
    digits = sum(c.isdigit() for c in merchant)
    return letters >= 3 and digits <= letters * 0.3


_LLM_PROMPT = """You normalize bank-transaction descriptors into merchant names.
Descriptor: {descriptor!r}
Respond with JSON: {{"merchant": "<canonical merchant name, title case>"}}"""


async def normalize_merchants(session: AsyncSession, llm: Classifier | None) -> int:
    aliases = {
        a.raw_descriptor: a.merchant
        for a in (await session.execute(select(MerchantAlias))).scalars()
    }
    txns = (
        (await session.execute(select(Transaction).where(Transaction.merchant.is_(None))))
        .scalars()
        .all()
    )
    count = 0
    for txn in txns:
        raw = txn.description
        if raw in aliases:
            txn.merchant = aliases[raw]
            count += 1
            continue
        candidate = rule_normalize(raw)
        source = AliasSource.rule
        if not looks_clean(candidate):
            answer = await llm.classify_json(_LLM_PROMPT.format(descriptor=raw)) if llm else None
            llm_merchant = answer.get("merchant") if answer else None
            llm_merchant = llm_merchant.strip() if isinstance(llm_merchant, str) else None
            if llm_merchant:
                candidate, source = llm_merchant, AliasSource.llm
            else:
                session.add(
                    ReviewItem(
                        kind=ReviewKind.merchant,
                        question=f"What merchant is {raw!r}?",
                        payload={"descriptor": raw, "fallback": candidate},
                    )
                )
        txn.merchant = candidate
        aliases[raw] = candidate
        session.add(MerchantAlias(raw_descriptor=raw, merchant=candidate, source=source))
        count += 1
    await session.commit()
    return count


_MIN_TARGET_LETTERS = 4
_MIN_PLACE_NAMES = 3
# a prefix followed by this many different non-place tails is a payment rail or a
# generic label ("Zelle Payment To …"), not one merchant
_MAX_TAILS = 3
# a feed that glues the city onto the merchant ("ACME UNIVERSITYRIVERTON CA"): a glued
# tail has at least this many letters ("Monthly" is not "Month" plus a place), and a
# multi-word name needs this many different glued tails before they read as places
# rather than one longer word ("Corner Storehouse" is not "Corner Store" plus a place)
_MIN_GLUED_LETTERS = 4
_MIN_GLUED_TAILS = 2
# a per-charge code hyphenated onto the merchant ("ACMEAPP-QWERTYUI"); a code with a digit
# is already a reference token, so only letters-only codes reach the merge pass
_CHARGE_CODE = re.compile(r"^([a-z]{4,})-([0-9a-z]{8})$", re.IGNORECASE)
# one hyphenated name may be a real one ("Corner-Sandwich"); different codes behind one
# merchant are per-charge references
_MIN_CODES = 2
# a short reference code among a name's words ("Acme Cloud Q1W2E3"): letters and digits
# mixed, 4+ characters (_REF_TOKEN strips the 8+ character ones with a digit)
_SHORT_CODE = re.compile(r"(?=[a-z]*\d)(?=\d*[a-z])[0-9a-z]{4,}", re.IGNORECASE)
# an ordinal is a street ("21St"), and a masked account number ("Xxx1234") names the
# account it pays, so neither is a per-charge code
_NOT_A_CODE = re.compile(r"\d+(?:st|nd|rd|th)|x+\d+", re.IGNORECASE)


def _tokens(name: str) -> tuple[str, ...]:
    """Comparison tokens: lower-cased, leading digits dropped ("0Elmton" -> "elmton")."""
    return tuple(t for raw in name.lower().split() if (t := raw.lstrip("0123456789")))


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


def _descriptor_codes(descriptors: Iterable[str]) -> dict[str, set[str]]:
    """merchant -> the per-charge codes hyphenated onto it in raw descriptors, digit codes
    included ("ACMEAPP-QW3RTYUI" -> {"acmeapp": {"qw3rtyui"}}): the rules strip a digit
    code as a reference token, so only the raw text shows the merchant bills under codes."""
    codes: dict[str, set[str]] = {}
    for raw in descriptors:
        s = raw.strip()
        while (stripped := _PREFIXES.sub("", s)) != s:
            s = stripped
        if (match := _CHARGE_CODE.match(s.partition(" ")[0])) is not None:
            codes.setdefault(match[1].lower(), set()).add(match[2].lower())
    return codes


def _charge_codes(names: set[str], seen: dict[str, set[str]]) -> dict[str, str]:
    """name -> the name without its per-charge code ("Acmeapp-Qwertyui" -> "Acmeapp"),
    for a merchant seen with 2+ different codes, counting the codes in `seen`."""
    parts: dict[str, tuple[str, str]] = {}  # name -> (merchant, rest of the name)
    codes = {merchant: set(found) for merchant, found in seen.items()}
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


def _is_code(word: str) -> bool:
    return _SHORT_CODE.fullmatch(word) is not None and _NOT_A_CODE.fullmatch(word) is None


def _short_codes(names: set[str], others: set[str]) -> dict[str, str]:
    """name -> the name without its short code ("Acme Cloud Q1W2E3" -> "Acme Cloud"),
    for names identical but for one code, seen with 2+ different codes. Right behind a
    prefix that 3+ different words follow (a payment rail), a code may be all that names
    the payee, so it stays; `others` count toward that but never shed a code."""
    after: dict[tuple[str, ...], set[str]] = {}  # prefix -> the non-code words after it
    for name in names | others:
        words = name.lower().split()
        for i, word in enumerate(words):
            if not _is_code(word):
                after.setdefault(tuple(words[:i]), set()).add(word)
    slots: dict[tuple[tuple[str, ...], tuple[str, ...]], set[str]] = {}  # name sans code
    found: dict[str, tuple[tuple[tuple[str, ...], tuple[str, ...]], str]] = {}
    for name in names:
        words = name.split()
        coded = [i for i, word in enumerate(words) if _is_code(word)]
        if len(coded) != 1:
            continue
        i = coded[0]
        lower = [word.lower() for word in words]
        slot = (tuple(lower[:i]), tuple(lower[i + 1 :]))
        slots.setdefault(slot, set()).add(lower[i])
        found[name] = (slot, " ".join(words[:i] + words[i + 1 :]))
    return {
        name: bare
        for name, (slot, bare) in found.items()
        if len(slots[slot]) >= _MIN_CODES
        and len(after.get(slot[0], ())) < _MAX_TAILS
        and _letters(bare) >= _MIN_TARGET_LETTERS
    }


def merge_targets(
    names: Iterable[str], targets_only: Iterable[str] = (), descriptors: Iterable[str] = ()
) -> dict[str, str]:
    """name -> the shorter name it merges into (design 2026-10-13 §1).

    A multi-token name absorbs any name it is a token prefix of; a single-token name
    absorbs only itself plus one *place* — a token that follows 3+ distinct existing
    names, learned from the data. Brand + product names therefore stay apart. A prefix
    with 3+ different non-place tails absorbs only its place variants; the tails count
    the names as they arrived, before any code below is shed. A name whose last
    word is a shorter name's last word with a place glued on ("Acme Universityriverton")
    merges into it when the glued tail is a place, or when a multi-word name shows 2+
    different glued tails, unless that name is a generic prefix (3+ different non-place
    tails), where only a place folds. A per-charge code ("Acmeapp-Qwertyui") is dropped
    once 2+ different codes share the merchant, counting the digit codes the rules
    stripped from `descriptors` (raw bank text). Names identical but for one short code
    ("Acme Cloud Q1W2E3") fold into the name without it once 2+ different codes appear
    there, unless the code follows a payment rail. Names in `targets_only` can absorb
    others but never merge themselves and teach no places.
    """
    arrived = set(names)
    sources = set(arrived)
    fixed = set(targets_only)
    coded = _charge_codes(sources, _descriptor_codes(descriptors))
    sources = (sources - coded.keys()) | set(coded.values())
    short = _short_codes(sources, fixed)
    sources = (sources - short.keys()) | set(short.values())
    keyed = {n: _tokens(n) for n in sorted(sources | fixed)}
    by_tokens: dict[tuple[str, ...], str] = {}
    for name, toks in keyed.items():
        by_tokens.setdefault(toks, name)
    followers: dict[str, set[tuple[str, ...]]] = {}
    for name, toks in keyed.items():
        if name in sources and len(toks) >= 2 and toks[:-1] in by_tokens:
            followers.setdefault(toks[-1], set()).add(toks[:-1])
    places = {t for t, prefixes in followers.items() if len(prefixes) >= _MIN_PLACE_NAMES}

    def is_place(tail: tuple[str, ...]) -> bool:
        return len(tail) == 1 and tail[0] in places

    tails: dict[tuple[str, ...], set[tuple[str, ...]]] = {}
    # names count as they arrived too: a payee shedding its codes must not leave a
    # payment rail with too few tails to look like one
    for toks in [*(keyed[n] for n in keyed if n in sources), *map(_tokens, arrived)]:
        for size in range(1, len(toks)):
            if toks[:size] in by_tokens and not is_place(toks[size:]):
                tails.setdefault(toks[:size], set()).add(toks[size:])
    generic = {prefix for prefix, found in tails.items() if len(found) >= _MAX_TAILS}
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

    targets: dict[str, str] = {}
    for name, toks in keyed.items():
        if not toks or name not in sources:
            continue
        for size in range(1, len(toks)):  # shortest qualifying prefix wins
            target = by_tokens.get(toks[:size])
            if target is None or sum(c.isalpha() for c in target) < _MIN_TARGET_LETTERS:
                continue
            if toks[:size] in generic and not is_place(toks[size:]):
                continue
            if size >= 2 or (len(toks) == 2 and toks[1] in places):
                targets[name] = target
                break
        else:
            if by_tokens[toks] != name:  # same tokens, different spelling
                targets[name] = by_tokens[toks]
            elif (glued_into := glued_target(name)) is not None:
                targets[name] = glued_into
    for name, bare in (coded | short).items():  # before chains, so a code follows its merchant
        targets[name] = bare
    for name in list(targets):  # resolve chains: A -> B -> C becomes A -> C
        seen = {name}
        while (nxt := targets.get(targets[name])) is not None and nxt not in seen:
            seen.add(nxt)
            targets[name] = nxt
    return targets


async def renormalize_merchants(session: AsyncSession) -> int:
    """Re-apply rules to non-manual aliases, then merge names (design 2026-10-13 §1).

    Runs every sync and is idempotent. Renames follow onto series and recurring
    decisions; an old name whose aliases now split across several names is ambiguous,
    so its series is left for detection to untag and end, and a name some alias still
    carries keeps its series and decisions. A merchant-level decision that follows a
    merge of two or more old names names the old name's median amount, so it covers only
    that bill; a pure rename leaves it merchant-level. Manual
    aliases, LLM aliases whose rule output is unclean, and LLM groups whose aliases
    would not all land on one name are never rewritten, but their names are merge
    targets.
    """
    aliases = list((await session.execute(select(MerchantAlias))).scalars())
    base: dict[int, str] = {}
    fixed: set[int] = set()  # aliases that keep their name: manual, or LLM-only names
    for alias in aliases:
        candidate = rule_normalize(alias.raw_descriptor)
        clean = looks_clean(candidate)
        base[alias.id] = (
            candidate if clean and alias.source != AliasSource.manual else alias.merchant
        )
        # an LLM name with no clean rule output is the LLM's call: merging it would
        # rewrite the name the next run learns places from, and flip merges back
        if alias.source == AliasSource.manual or (alias.source == AliasSource.llm and not clean):
            fixed.add(alias.id)
    # an LLM group (aliases sharing one LLM name) is rewritten only when every alias
    # lands on one final name; otherwise rewriting would split what the LLM unified
    llm_groups: dict[str, list[MerchantAlias]] = {}
    for alias in aliases:
        if alias.source == AliasSource.llm:
            llm_groups.setdefault(alias.merchant, []).append(alias)
    while True:  # fixing a group changes the merge input, so repeat until it stops growing
        targets = merge_targets(
            (base[a.id] for a in aliases if a.id not in fixed),
            targets_only=(base[a.id] for a in aliases if a.id in fixed),
            descriptors=(a.raw_descriptor for a in aliases),
        )
        grown = False
        for group in llm_groups.values():
            if any(a.id not in fixed for a in group) and (
                len(
                    {
                        a.merchant if a.id in fixed else targets.get(base[a.id], base[a.id])
                        for a in group
                    }
                )
                > 1
            ):
                for a in group:
                    fixed.add(a.id)
                    base[a.id] = a.merchant  # a kept alias targets its own name, not the rules'
                grown = True
        if not grown:
            break
    rename: dict[str, str | None] = {}  # old merchant -> new (None = ambiguous)
    by_target: dict[str, list[str]] = {}
    changed = 0
    came_from: dict[str, set[str]] = {}  # final name -> the old names its aliases carried
    for alias in aliases:
        old = alias.merchant
        final = old if alias.id in fixed else targets.get(base[alias.id], base[alias.id])
        came_from.setdefault(final, set()).add(old)
        if final == old:
            continue
        rename[old] = None if rename.get(old, final) != final else final
        if alias.source == AliasSource.llm and base[alias.id] != old:
            alias.source = AliasSource.rule  # the clean rule output replaced the LLM's name
        alias.merchant = final
        by_target.setdefault(final, []).append(alias.raw_descriptor)
        changed += 1
    # a name some alias still carries keeps its own series and decisions
    carried = {alias.merchant for alias in aliases}
    renames = {old: new for old, new in rename.items() if new is not None and old not in carried}
    # a merchant-level decision about the old name now addresses only that name's bill:
    # left merchant-level, it would cover every bill of the merged merchant (design §4)
    old_medians: dict[tuple[str, str], int] = {}
    if renames:
        amounts: dict[tuple[str, str], list[int]] = {}
        for txn in (
            await session.execute(select(Transaction).where(Transaction.merchant.in_(renames)))
        ).scalars():
            direction = "outflow" if txn.amount_cents < 0 else "inflow"
            amounts.setdefault((str(txn.merchant), direction), []).append(abs(txn.amount_cents))
        old_medians = {key: round(statistics.median(cents)) for key, cents in amounts.items()}
    for final, raws in by_target.items():
        await session.execute(
            update(Transaction).where(Transaction.description.in_(raws)).values(merchant=final)
        )
    if renames:
        for series in (
            await session.execute(
                select(RecurringSeries).where(RecurringSeries.merchant.in_(renames))
            )
        ).scalars():
            series.merchant = renames[series.merchant]
        for item in (
            await session.execute(
                select(ReviewItem).where(ReviewItem.kind == ReviewKind.recurring_cluster)
            )
        ).scalars():
            merchant = item.payload.get("merchant")
            if not isinstance(merchant, str) or merchant not in renames:
                continue
            payload = {**item.payload, "merchant": renames[merchant]}
            ref = bill_ref(item.payload)
            if (
                ref is not None
                and ref[2] is None
                and len(came_from[renames[merchant]]) > 1
                and old_medians.get(ref[:2], 0) > 0
            ):
                payload["amount_cents"] = old_medians[ref[:2]]
            item.payload = payload
        await move_pass_through_rulings(session, renames)
    await session.commit()
    return changed


async def move_pass_through_rulings(session: AsyncSession, renames: dict[str, str]) -> None:
    """Point the pass-through rulings on each old merchant name at its new one; the
    caller commits, and passes only names no transaction carries any more."""
    for item in (
        await session.execute(select(ReviewItem).where(ReviewItem.kind == ReviewKind.pass_through))
    ).scalars():
        merchant = item.payload.get("merchant")
        if isinstance(merchant, str) and merchant in renames:
            item.payload = {**item.payload, "merchant": renames[merchant]}
