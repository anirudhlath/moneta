import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.llm import Classifier
from moneta.models import AliasSource, MerchantAlias, ReviewItem, ReviewKind, Transaction

_PREFIXES = re.compile(r"^(sq \*|tst\*\s*|paypal \*|py \*|amzn mktp us\*)", re.IGNORECASE)
_STORE_NUM = re.compile(r"(#\d+|\b\d{3,}\b)")
# payment-reference tokens: 8+ alphanumeric chars containing at least one digit
_REF_TOKEN = re.compile(r"\b(?=[0-9a-zA-Z]*\d)[0-9a-zA-Z]{8,}\b")
_SPACES = re.compile(r"\s+")


def rule_normalize(descriptor: str) -> str:
    s = _PREFIXES.sub("", descriptor.strip())
    s = _REF_TOKEN.sub("", s)
    s = _STORE_NUM.sub("", s)
    s = _SPACES.sub(" ", s).strip(" -*:")
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


async def renormalize_merchants(session: AsyncSession) -> int:
    """Re-apply improved rules to existing non-manual aliases.

    Rewrites the affected transactions' merchants and renames series where the
    old→new mapping is unambiguous and collision-free. Manual aliases are never touched.
    """
    from moneta.models import RecurringSeries

    aliases = (
        (
            await session.execute(
                select(MerchantAlias).where(MerchantAlias.source != AliasSource.manual)
            )
        )
        .scalars()
        .all()
    )
    rename: dict[str, str | None] = {}  # old merchant -> new (None = ambiguous)
    changed = 0
    for alias in aliases:
        candidate = rule_normalize(alias.raw_descriptor)
        if not looks_clean(candidate) or candidate == alias.merchant:
            continue
        old = alias.merchant
        rename[old] = None if rename.get(old, candidate) != candidate else candidate
        alias.merchant = candidate
        alias.source = AliasSource.rule
        for txn in (
            await session.execute(
                select(Transaction).where(Transaction.description == alias.raw_descriptor)
            )
        ).scalars():
            txn.merchant = candidate
        changed += 1

    series_rows = (await session.execute(select(RecurringSeries))).scalars().all()
    taken = {(s.merchant, s.direction) for s in series_rows}
    for s in series_rows:
        new = rename.get(s.merchant)
        if new and (new, s.direction) not in taken:
            taken.discard((s.merchant, s.direction))
            s.merchant = new
            taken.add((new, s.direction))
    await session.commit()
    return changed
