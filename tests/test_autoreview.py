from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import (
    AccountType,
    Direction,
    MerchantAlias,
    RecurringSeries,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
    SeriesStatus,
    Transaction,
    TransferLink,
)
from moneta.pipelines.recurring import detect_recurring
from moneta.pipelines.review import (
    VerifyStats,
    apply_resolution,
    autoreview_items,
    verify_series,
)
from tests.factories import make_account, make_series, make_txn


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
    await detect_recurring(session, llm=None, today=date(2026, 7, 1))  # opens the review item
    assert len(await _open_items(session)) == 1
    llm = ScriptedLLM({"Odd Timing Co": {"is_recurring": True, "confident": True}})
    assert await autoreview_items(session, llm) == 1
    # consumes the force map
    stats = await detect_recurring(session, llm=None, today=date(2026, 7, 1))
    assert stats.new_series == 1


async def _series_with_occurrences(session: AsyncSession) -> RecurringSeries:
    acct = await make_account(session)
    series = await make_series(session, merchant="Netflix")
    await make_txn(
        session,
        acct,
        amount_cents=-1599,
        merchant="Netflix",
        posted_on=date(2026, 6, 15),
        series_id=series.id,
    )
    return series


async def test_verify_confident_yes_writes_resolved_ledger_item(session: AsyncSession) -> None:
    await _series_with_occurrences(session)
    llm = ScriptedLLM({"Netflix": {"is_recurring": True, "confident": True}})
    stats = await verify_series(session, llm)
    assert stats == VerifyStats(verified=1, flagged=0)
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.status == ReviewStatus.resolved
    assert item.resolution == {"is_recurring": True, "resolved_by": "llm"}
    # settled: a second run asks nothing
    assert await verify_series(session, llm) == VerifyStats()
    assert len(llm.prompts) == 1


async def test_verify_prompt_carries_amounts_and_dates(session: AsyncSession) -> None:
    await _series_with_occurrences(session)
    llm = ScriptedLLM({"Netflix": {"is_recurring": True, "confident": True}})
    await verify_series(session, llm)
    assert "15.99" in llm.prompts[0] and "2026-06-15" in llm.prompts[0]


async def test_verify_unconfident_flags_for_human(session: AsyncSession) -> None:
    series = await _series_with_occurrences(session)
    llm = ScriptedLLM({"Netflix": {"is_recurring": True, "confident": False}})
    stats = await verify_series(session, llm)
    assert stats == VerifyStats(verified=0, flagged=1)
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.status == ReviewStatus.open
    assert item.payload["llm_flagged"] is True
    assert series.status == SeriesStatus.active  # keeps counting until a human rules


async def test_verify_confident_no_flags_rather_than_suppresses(session: AsyncSession) -> None:
    series = await _series_with_occurrences(session)
    llm = ScriptedLLM({"Netflix": {"is_recurring": False, "confident": True}})
    stats = await verify_series(session, llm)
    assert stats == VerifyStats(verified=0, flagged=1)
    assert series.status == SeriesStatus.active  # the LLM never suppresses determinism
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.status == ReviewStatus.open


async def test_verify_skips_merchants_with_existing_items(session: AsyncSession) -> None:
    await _series_with_occurrences(session)
    session.add(
        ReviewItem(
            kind=ReviewKind.recurring_cluster,
            question="Is 'Netflix' a recurring bill?",
            payload={"merchant": "Netflix", "direction": "outflow"},
        )
    )
    await session.flush()
    llm = ScriptedLLM({"Netflix": {"is_recurring": True, "confident": True}})
    assert await verify_series(session, llm) == VerifyStats()
    assert llm.prompts == []


async def test_verify_skips_ended_series(session: AsyncSession) -> None:
    await make_series(session, merchant="Old Gym", status=SeriesStatus.ended)
    llm = ScriptedLLM({"Old Gym": {"is_recurring": True, "confident": True}})
    assert await verify_series(session, llm) == VerifyStats()
    assert llm.prompts == []


async def test_verify_without_llm_is_noop(session: AsyncSession) -> None:
    await _series_with_occurrences(session)
    assert await verify_series(session, None) == VerifyStats()
    assert (await session.execute(select(ReviewItem))).scalar_one_or_none() is None


async def test_autoreview_skips_llm_flagged_items(session: AsyncSession) -> None:
    session.add(
        ReviewItem(
            kind=ReviewKind.recurring_cluster,
            question="Is 'Costco' a recurring bill?",
            payload={"merchant": "Costco", "direction": "outflow", "llm_flagged": True},
        )
    )
    await session.flush()
    llm = ScriptedLLM({"Costco": {"is_recurring": True, "confident": True}})
    assert await autoreview_items(session, llm) == 0
    assert llm.prompts == []  # the LLM already looked once; re-asking is circular
    assert len(await _open_items(session)) == 1


async def test_not_recurring_resolution_ends_live_series(session: AsyncSession) -> None:
    series = await make_series(session, merchant="Costco")
    item = ReviewItem(
        kind=ReviewKind.recurring_cluster,
        question="Is 'Costco' a recurring bill?",
        payload={"merchant": "Costco", "direction": "outflow"},
    )
    session.add(item)
    await session.flush()
    await apply_resolution(session, item, {"is_recurring": False})
    assert series.status == SeriesStatus.ended
    assert item.status == ReviewStatus.resolved


async def test_not_recurring_resolution_leaves_other_direction_alone(
    session: AsyncSession,
) -> None:
    series = await make_series(session, merchant="Costco", direction=Direction.inflow)
    item = ReviewItem(
        kind=ReviewKind.recurring_cluster,
        question="Is 'Costco' a recurring bill?",
        payload={"merchant": "Costco", "direction": "outflow"},
    )
    session.add(item)
    await session.flush()
    await apply_resolution(session, item, {"is_recurring": False})
    assert series.status == SeriesStatus.active
