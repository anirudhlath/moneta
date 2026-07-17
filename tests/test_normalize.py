from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import MerchantAlias, ReviewItem, Transaction
from moneta.pipelines.normalize import looks_clean, normalize_merchants, rule_normalize
from tests.factories import make_account, make_txn


class FakeLLM:
    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    async def classify_json(self, prompt: str) -> dict[str, Any] | None:
        self.calls.append(prompt)
        for needle, merchant in self.answers.items():
            if needle in prompt:
                return {"merchant": merchant}
        return None


def test_rule_normalize() -> None:
    assert rule_normalize("NETFLIX.COM") == "Netflix.Com"
    assert rule_normalize("SQ *BLUE BOTTLE #1234") == "Blue Bottle"
    assert rule_normalize("TST* JOES  DINER 0042") == "Joes Diner"
    assert rule_normalize("PAYPAL *SPOTIFY") == "Spotify"


def test_rule_normalize_keeps_hyphen_joined_digit_runs() -> None:
    # a hyphen-joined digit run (e.g. a vanity phone number) is not a store number
    # and must survive normalization intact.
    assert rule_normalize("1-800-FLOWERS.COM") == "1-800-Flowers.Com"
    assert rule_normalize("BLUE BOTTLE #1234") == "Blue Bottle"
    assert rule_normalize("STORE 4521") == "Store"


def test_looks_clean() -> None:
    assert looks_clean("Netflix.Com")
    assert not looks_clean("X4529182")
    assert not looks_clean("")


async def test_normalize_uses_rules_and_caches(session: AsyncSession) -> None:
    acct = await make_account(session)
    await make_txn(session, acct, description="NETFLIX.COM")
    await make_txn(session, acct, description="NETFLIX.COM")
    n = await normalize_merchants(session, llm=None)
    assert n == 2
    txns = (await session.execute(select(Transaction))).scalars().all()
    assert all(t.merchant == "Netflix.Com" for t in txns)
    alias = (await session.execute(select(MerchantAlias))).scalar_one()
    assert alias.raw_descriptor == "NETFLIX.COM" and alias.source == "rule"


async def test_normalize_falls_back_to_llm(session: AsyncSession) -> None:
    acct = await make_account(session)
    await make_txn(session, acct, description="X4529182 84756")
    llm = FakeLLM({"X4529182": "Mystery Gym"})
    await normalize_merchants(session, llm=llm)
    txn = (await session.execute(select(Transaction))).scalar_one()
    assert txn.merchant == "Mystery Gym"
    alias = (await session.execute(select(MerchantAlias))).scalar_one()
    assert alias.source == "llm"


async def test_normalize_opens_review_without_llm(session: AsyncSession) -> None:
    acct = await make_account(session)
    await make_txn(session, acct, description="X4529182 84756")
    await normalize_merchants(session, llm=None)
    txn = (await session.execute(select(Transaction))).scalar_one()
    assert txn.merchant is not None  # rule fallback still applied
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.kind == "merchant"


class BlankLLM:
    async def classify_json(self, prompt: str) -> dict[str, Any] | None:
        return {"merchant": "  "}


class MalformedLLM:
    async def classify_json(self, prompt: str) -> dict[str, Any] | None:
        return {"merchant": {"name": "X"}}  # truthy non-str must degrade, never crash sync


async def test_normalize_rejects_non_string_llm_merchant(session: AsyncSession) -> None:
    acct = await make_account(session)
    await make_txn(session, acct, description="X4529182 84756")
    await normalize_merchants(session, llm=MalformedLLM())
    txn = (await session.execute(select(Transaction))).scalar_one()
    assert txn.merchant == rule_normalize("X4529182 84756")  # rule fallback, not a dict
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.kind == "merchant"
    alias = (await session.execute(select(MerchantAlias))).scalar_one()
    assert alias.source == "rule"


async def test_normalize_rejects_empty_llm_merchant(session: AsyncSession) -> None:
    acct = await make_account(session)
    await make_txn(session, acct, description="X4529182 84756")
    await normalize_merchants(session, llm=BlankLLM())
    txn = (await session.execute(select(Transaction))).scalar_one()
    assert txn.merchant == rule_normalize("X4529182 84756")  # rule fallback, not blank
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.kind == "merchant"
    alias = (await session.execute(select(MerchantAlias))).scalar_one()
    assert alias.source == "rule"


async def test_alias_cache_skips_llm(session: AsyncSession) -> None:
    acct = await make_account(session)
    await make_txn(session, acct, description="X4529182 84756")
    llm = FakeLLM({"X4529182": "Mystery Gym"})
    await normalize_merchants(session, llm=llm)
    await make_txn(session, acct, description="X4529182 84756")
    await normalize_merchants(session, llm=llm)
    assert len(llm.calls) == 1  # second run served from MerchantAlias


def test_rule_normalize_strips_reference_tokens() -> None:
    # payment-reference tokens (8+ chars containing a digit) must not fragment merchants
    variants = [
        "BILT PAYMENT BILTRENT f3d9bb17a1f74fa WEB ID: 9999918544",
        "BILT PAYMENT BILTRENT 262f2feddc034a5 WEB ID: 9999918544",
        "BILT PAYMENT BILTRENT 311233ba29b5469 WEB ID: 9999918544",
    ]
    names = {rule_normalize(v) for v in variants}
    assert len(names) == 1
    assert names.pop() == "Bilt Payment Biltrent Web Id"


async def test_renormalize_merges_fragmented_merchants(session: AsyncSession) -> None:
    from moneta.models import AliasSource, RecurringSeries
    from moneta.pipelines.normalize import renormalize_merchants

    acct = await make_account(session)
    raws = [
        "BILT PAYMENT BILTRENT f3d9bb17a1f74fa WEB ID: 9999918544",
        "BILT PAYMENT BILTRENT 262f2feddc034a5 WEB ID: 9999918544",
        "BILT PAYMENT BILTRENT 311233ba29b5469 WEB ID: 9999918544",
    ]
    old_names = ["Bilt Payment Biltrent F3D9Bb17A1F74Fa Web Id:", "Bilt 262F2F", "Bilt Rent"]
    for raw, old in zip(raws, old_names, strict=True):
        await make_txn(session, acct, description=raw, merchant=old, amount_cents=-249004)
        session.add(
            MerchantAlias(
                raw_descriptor=raw,
                merchant=old,
                source=AliasSource.llm if old == "Bilt Rent" else AliasSource.rule,
            )
        )
    # a manual alias must never be touched
    await make_txn(session, acct, description="SOME RAW 12345678x", merchant="My Custom Name")
    session.add(
        MerchantAlias(
            raw_descriptor="SOME RAW 12345678x",
            merchant="My Custom Name",
            source=AliasSource.manual,
        )
    )
    await session.flush()

    changed = await renormalize_merchants(session)
    assert changed == 3
    txns = (await session.execute(select(Transaction))).scalars().all()
    bilt_names = {t.merchant for t in txns if t.description.startswith("BILT")}
    assert bilt_names == {"Bilt Payment Biltrent Web Id"}
    manual = next(t for t in txns if t.description == "SOME RAW 12345678x")
    assert manual.merchant == "My Custom Name"
    assert (await session.execute(select(RecurringSeries))).scalars().all() == []
