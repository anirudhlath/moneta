from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import AccountType, ReviewItem, TransferLink
from moneta.pipelines.transfers import link_transfers
from tests.factories import make_account, make_txn


async def test_clean_pair_auto_links(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    savings = await make_account(session, type=AccountType.savings)
    out = await make_txn(
        session,
        checking,
        amount_cents=-50000,
        posted_on=date(2026, 7, 1),
        description="ONLINE TRANSFER TO SAVINGS",
    )
    inn = await make_txn(
        session,
        savings,
        amount_cents=50000,
        posted_on=date(2026, 7, 2),
        description="TRANSFER FROM CHECKING",
    )
    stats = await link_transfers(session, llm=None)
    assert stats.linked == 1
    link = (await session.execute(select(TransferLink))).scalar_one()
    assert link.outflow_id == out.id and link.inflow_id == inn.id
    assert link.method == "rule" and link.confidence >= 0.8


async def test_ambiguous_goes_to_review_without_llm(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    savings = await make_account(session, type=AccountType.savings)
    credit = await make_account(session, type=AccountType.credit)
    await make_txn(
        session,
        checking,
        amount_cents=-50000,
        posted_on=date(2026, 7, 1),
        description="ACH TRANSFER",
    )
    await make_txn(
        session, savings, amount_cents=50000, posted_on=date(2026, 7, 2), description="DEPOSIT"
    )
    await make_txn(
        session,
        credit,
        amount_cents=50000,
        posted_on=date(2026, 7, 2),
        description="PAYMENT RECEIVED",
    )
    stats = await link_transfers(session, llm=None)
    assert stats.linked == 0 and stats.review == 1
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.kind == "transfer_pair"


async def test_ambiguous_resolved_by_llm(session: AsyncSession) -> None:
    checking = await make_account(session, type=AccountType.checking)
    savings = await make_account(session, type=AccountType.savings)
    credit = await make_account(session, type=AccountType.credit)
    await make_txn(
        session,
        checking,
        amount_cents=-50000,
        posted_on=date(2026, 7, 1),
        description="ACH TRANSFER",
    )
    inn = await make_txn(
        session, savings, amount_cents=50000, posted_on=date(2026, 7, 2), description="DEPOSIT"
    )
    await make_txn(
        session,
        credit,
        amount_cents=50000,
        posted_on=date(2026, 7, 2),
        description="PAYMENT RECEIVED",
    )

    class PickLLM:
        async def classify_json(self, prompt: str) -> dict[str, Any] | None:
            return {"inflow_id": inn.id}

    stats = await link_transfers(session, llm=PickLLM())
    assert stats.linked == 1
    link = (await session.execute(select(TransferLink))).scalar_one()
    assert link.inflow_id == inn.id and link.method == "llm"


async def test_no_double_linking(session: AsyncSession) -> None:
    a = await make_account(session, type=AccountType.checking)
    b = await make_account(session, type=AccountType.savings)
    await make_txn(
        session, a, amount_cents=-50000, posted_on=date(2026, 7, 1), description="TRANSFER"
    )
    await make_txn(
        session, a, amount_cents=-50000, posted_on=date(2026, 7, 1), description="TRANSFER"
    )
    await make_txn(
        session, b, amount_cents=50000, posted_on=date(2026, 7, 1), description="TRANSFER"
    )
    stats = await link_transfers(session, llm=None)
    assert stats.linked == 1  # one inflow can satisfy only one outflow
    links = (await session.execute(select(TransferLink))).scalars().all()
    ids = [link.inflow_id for link in links] + [link.outflow_id for link in links]
    assert len(ids) == len(set(ids))


async def test_far_dates_not_candidates(session: AsyncSession) -> None:
    a = await make_account(session, type=AccountType.checking)
    b = await make_account(session, type=AccountType.savings)
    await make_txn(
        session, a, amount_cents=-50000, posted_on=date(2026, 7, 1), description="TRANSFER"
    )
    await make_txn(
        session, b, amount_cents=50000, posted_on=date(2026, 7, 20), description="TRANSFER"
    )
    stats = await link_transfers(session, llm=None)
    assert stats.linked == 0 and stats.review == 0


async def test_rerun_is_idempotent(session: AsyncSession) -> None:
    a = await make_account(session, type=AccountType.checking)
    b = await make_account(session, type=AccountType.savings)
    await make_txn(
        session,
        a,
        amount_cents=-50000,
        posted_on=date(2026, 7, 1),
        description="TRANSFER TO SAVINGS",
    )
    await make_txn(
        session,
        b,
        amount_cents=50000,
        posted_on=date(2026, 7, 1),
        description="TRANSFER FROM CHECKING",
    )
    await link_transfers(session, llm=None)
    stats = await link_transfers(session, llm=None)
    assert stats.linked == 0
    assert len((await session.execute(select(TransferLink))).scalars().all()) == 1
