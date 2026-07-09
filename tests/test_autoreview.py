from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import (
    AccountType,
    MerchantAlias,
    ReviewItem,
    ReviewStatus,
    Transaction,
    TransferLink,
)
from moneta.pipelines.recurring import detect_recurring
from moneta.pipelines.review import autoreview_items
from tests.factories import make_account, make_txn


class ScriptedLLM:
    """Returns a canned answer when a needle appears in the prompt."""

    def __init__(self, answers: dict[str, dict[str, Any]]) -> None:
        self.answers = answers
        self.prompts: list[str] = []

    async def classify_json(self, prompt: str) -> dict[str, Any] | None:
        self.prompts.append(prompt)
        for needle, answer in self.answers.items():
            if needle in prompt:
                return answer
        return None


async def _open_items(session: AsyncSession) -> list[ReviewItem]:
    return list(
        (
            await session.execute(select(ReviewItem).where(ReviewItem.status == ReviewStatus.open))
        ).scalars()
    )


async def test_confident_merchant_answer_resolves(session: AsyncSession) -> None:
    acct = await make_account(session)
    await make_txn(session, acct, description="X99Q8 GYM 8842", merchant="X99Q8 Gym")
    session.add(
        ReviewItem(
            kind="merchant",
            question="What merchant is 'X99Q8 GYM 8842'?",
            payload={"descriptor": "X99Q8 GYM 8842", "fallback": "X99Q8 Gym"},
        )
    )
    await session.flush()
    llm = ScriptedLLM({"X99Q8": {"merchant": "Equinox Gym", "confident": True}})
    resolved = await autoreview_items(session, llm)
    assert resolved == 1
    assert await _open_items(session) == []
    txn = (await session.execute(select(Transaction))).scalar_one()
    assert txn.merchant == "Equinox Gym"
    alias = (await session.execute(select(MerchantAlias))).scalar_one()
    assert alias.merchant == "Equinox Gym" and alias.source == "llm"
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.resolution is not None and item.resolution["resolved_by"] == "llm"


async def test_unconfident_answer_stays_open(session: AsyncSession) -> None:
    session.add(
        ReviewItem(
            kind="merchant",
            question="What merchant is 'ZZZ'?",
            payload={"descriptor": "ZZZ", "fallback": "Zzz"},
        )
    )
    await session.flush()
    llm = ScriptedLLM({"ZZZ": {"merchant": "Maybe Corp", "confident": False}})
    assert await autoreview_items(session, llm) == 0
    assert len(await _open_items(session)) == 1


async def test_confident_transfer_pick_creates_link(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    savings = await make_account(session, type=AccountType.savings)
    out = await make_txn(
        session, checking, amount_cents=-50000, posted_on=date(2026, 7, 1), description="ACH OUT"
    )
    inn = await make_txn(
        session, savings, amount_cents=50000, posted_on=date(2026, 7, 2), description="DEPOSIT IN"
    )
    session.add(
        ReviewItem(
            kind="transfer_pair",
            question="Which inflow?",
            payload={"outflow_id": out.id, "candidates": [inn.id]},
        )
    )
    await session.flush()
    llm = ScriptedLLM({"ACH OUT": {"inflow_id": inn.id, "confident": True}})
    assert await autoreview_items(session, llm) == 1
    link = (await session.execute(select(TransferLink))).scalar_one()
    assert link.inflow_id == inn.id and link.method == "llm"


async def test_transfer_pick_outside_candidates_rejected(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    out = await make_txn(session, checking, amount_cents=-50000, description="ACH OUT")
    session.add(
        ReviewItem(
            kind="transfer_pair",
            question="Which inflow?",
            payload={"outflow_id": out.id, "candidates": [999]},
        )
    )
    await session.flush()
    llm = ScriptedLLM({"": {"inflow_id": 12345, "confident": True}})  # hallucinated id
    assert await autoreview_items(session, llm) == 0
    assert len(await _open_items(session)) == 1


async def test_confident_recurring_answer_feeds_next_detection(session: AsyncSession) -> None:
    acct = await make_account(session)
    # irregular-but-bill-like: stable amounts, cadence miss (gaps 10 / 45)
    for d in (date(2026, 4, 1), date(2026, 4, 11), date(2026, 5, 26)):
        await make_txn(session, acct, amount_cents=-30000, merchant="Odd Timing Co", posted_on=d)
    await detect_recurring(session, llm=None)  # opens the review item
    assert len(await _open_items(session)) == 1
    llm = ScriptedLLM({"Odd Timing Co": {"is_recurring": True, "confident": True}})
    assert await autoreview_items(session, llm) == 1
    stats = await detect_recurring(session, llm=None)  # consumes the force map
    assert stats.new_series == 1
