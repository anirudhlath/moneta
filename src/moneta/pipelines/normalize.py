import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.llm import Classifier
from moneta.models import AliasSource, MerchantAlias, ReviewItem, ReviewKind, Transaction

_PREFIXES = re.compile(r"^(sq \*|tst\*\s*|paypal \*|py \*|amzn mktp us\*)", re.IGNORECASE)
_STORE_NUM = re.compile(r"(#\d+|\b\d{3,}\b)")
_SPACES = re.compile(r"\s+")


def rule_normalize(descriptor: str) -> str:
    s = _PREFIXES.sub("", descriptor.strip())
    s = _STORE_NUM.sub("", s)
    s = _SPACES.sub(" ", s).strip(" -*")
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
            if answer and isinstance(answer.get("merchant"), str):
                candidate, source = answer["merchant"], AliasSource.llm
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
