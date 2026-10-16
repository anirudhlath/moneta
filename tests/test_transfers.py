from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.models import AccountType, ReviewItem, Transaction, TransferLink
from moneta.pipelines.reconcile import reconcile_provider_overlap
from moneta.pipelines.run import run_sync
from moneta.pipelines.transfers import PairScore, link_transfers, score_pair
from moneta.views.power import power_report
from tests.conftest import RecordingAdapter
from tests.factories import make_account, make_txn

TODAY = date(2026, 7, 15)


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


async def test_greedy_loser_opens_review_item(session: AsyncSession) -> None:
    """Two outflows compete for one inflow; the loser gets a ReviewItem carrying
    its original candidate ids instead of silently vanishing (design 2026-07-16 §7)."""
    a = await make_account(session, type=AccountType.checking)
    b = await make_account(session, type=AccountType.savings)
    out1 = await make_txn(
        session, a, amount_cents=-50000, posted_on=date(2026, 7, 1), description="TRANSFER"
    )
    out2 = await make_txn(
        session, a, amount_cents=-50000, posted_on=date(2026, 7, 1), description="TRANSFER"
    )
    inn = await make_txn(
        session, b, amount_cents=50000, posted_on=date(2026, 7, 1), description="TRANSFER"
    )
    stats = await link_transfers(session, llm=None)
    assert stats.linked == 1
    assert stats.review == 1
    link = (await session.execute(select(TransferLink))).scalar_one()
    winner_id = link.outflow_id
    assert winner_id in (out1.id, out2.id)
    loser_id = out2.id if winner_id == out1.id else out1.id
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.kind == "transfer_pair"
    assert item.payload["outflow_id"] == loser_id
    assert item.payload["candidates"] == [inn.id]


async def test_ambiguous_then_one_remaining_opens_review_not_link(session: AsyncSession) -> None:
    """An outflow that started with 2+ candidates never takes the fast auto-link
    path even when a higher-confidence rival's consumption leaves exactly one
    candidate standing (design 2026-07-16 §7). Confidences are tuned so the
    single remaining candidate clears _AUTO_LINK on its own — proving the guard
    is keyed on the ORIGINAL candidate count, not the post-filter one."""
    chk = await make_account(session, type=AccountType.checking)
    sav = await make_account(session, type=AccountType.savings)
    cred = await make_account(session, type=AccountType.credit)
    sav2 = await make_account(session, type=AccountType.savings)
    await make_txn(
        session,
        chk,
        amount_cents=-50000,
        posted_on=date(2026, 7, 1),
        description="TRANSFER TO SAVINGS",
    )
    inn1 = await make_txn(
        session,
        sav,
        amount_cents=50000,
        posted_on=date(2026, 7, 3),
        description="TRANSFER FROM CHECKING",
    )
    out_a = await make_txn(
        session, cred, amount_cents=-50000, posted_on=date(2026, 7, 8), description="MISC CHARGE"
    )
    inn2 = await make_txn(
        session,
        sav2,
        amount_cents=50000,
        posted_on=date(2026, 7, 10),
        description="TRANSFER REFUND",
    )
    stats = await link_transfers(session, llm=None)
    assert stats.linked == 1
    assert stats.review == 1
    link = (await session.execute(select(TransferLink))).scalar_one()
    assert link.inflow_id == inn1.id  # the higher-confidence pair auto-links first
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.payload["outflow_id"] == out_a.id
    assert item.payload["candidates"] == [inn2.id]


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


# (days apart, keyword, outflow leaves checking, score in tenths, rule-links)
_SCORE_TABLE = [
    (1, True, True, 10, True),
    (1, True, False, 9, True),
    (1, False, True, 8, False),  # reaches the threshold, but a rule link needs a keyword
    (1, False, False, 7, False),
    (3, True, True, 9, True),
    (3, True, False, 8, True),
    (3, False, True, 7, False),
    (3, False, False, 6, False),
    (4, True, True, 8, True),  # #46: float scoring summed this to 0.7999999999999999
    (4, True, False, 7, False),
    (4, False, True, 6, False),
    (4, False, False, 5, False),
]


@pytest.mark.parametrize(
    ("days_apart", "keyword", "from_checking", "tenths", "links"), _SCORE_TABLE
)
async def test_score_table_decides_rule_links(
    session: AsyncSession,
    days_apart: int,
    keyword: bool,
    from_checking: bool,
    tenths: int,
    links: bool,
) -> None:
    source = await make_account(
        session, type=AccountType.checking if from_checking else AccountType.credit
    )
    savings = await make_account(session, type=AccountType.savings)
    out = await make_txn(
        session,
        source,
        amount_cents=-10000,
        posted_on=date(2026, 7, 1),
        description="ONLINE TRANSFER TO SAVINGS" if keyword else "ACME STORE",
    )
    inn = await make_txn(
        session,
        savings,
        amount_cents=10000,
        posted_on=date(2026, 7, 1) + timedelta(days=days_apart),
        description="TRANSFER FROM CHECKING" if keyword else "CORNER CURRY",
    )
    assert score_pair(out, inn, source.type) == PairScore(tenths, keyword)
    stats = await link_transfers(session, llm=None)
    link = (await session.execute(select(TransferLink))).scalar_one_or_none()
    if links:
        assert stats.linked == 1
        assert link is not None
        assert (link.method, link.confidence) == ("rule", tenths / 10)
    else:
        assert link is None
        assert stats.review == 1  # a pair that doesn't auto-link is asked about, as before


async def test_card_payment_posting_four_days_apart_links_in_sync(session: AsyncSession) -> None:
    """#46: the card posts the payment credit four days before the checking debit.
    5 + 2 (keyword) + 1 (checking) is exactly the threshold; the payment must not
    also count as spend."""
    checking = await make_account(session, type=AccountType.checking)
    card = await make_account(session, type=AccountType.credit)
    inn = await make_txn(
        session,
        card,
        amount_cents=10000,
        posted_on=date(2026, 7, 1),
        description="PAYMENT RECEIVED",
    )
    out = await make_txn(
        session,
        checking,
        amount_cents=-10000,
        posted_on=date(2026, 7, 5),
        description="CARD PAYMENT",
    )
    await session.commit()
    report = await run_sync(session, [RecordingAdapter()], llm=None, today=TODAY)
    assert (report.transfers.linked, report.transfers.review) == (1, 0)
    link = (await session.execute(select(TransferLink))).scalar_one()
    assert (link.outflow_id, link.inflow_id, link.method) == (out.id, inn.id, "rule")
    assert link.confidence == 0.8
    assert (await power_report(session, TODAY)).spent_so_far_cents == 0


async def test_equal_scores_break_ties_by_lower_outflow_id(session: AsyncSession) -> None:
    """Two outflows score 9 tenths for one inflow: same-day from a card, and two days
    from checking. Float sums made these 0.8999999999999999 and 0.9; integer tenths
    tie exactly, and the lower outflow id links."""
    card = await make_account(session, type=AccountType.credit)
    checking = await make_account(session, type=AccountType.checking)
    savings = await make_account(session, type=AccountType.savings)
    first = await make_txn(
        session, card, amount_cents=-10000, posted_on=date(2026, 7, 3), description="TRANSFER OUT"
    )
    second = await make_txn(
        session,
        checking,
        amount_cents=-10000,
        posted_on=date(2026, 7, 1),
        description="TRANSFER OUT",
    )
    inn = await make_txn(
        session, savings, amount_cents=10000, posted_on=date(2026, 7, 3), description="TRANSFER IN"
    )
    stats = await link_transfers(session, llm=None)
    assert (stats.linked, stats.review) == (1, 1)
    link = (await session.execute(select(TransferLink))).scalar_one()
    assert (link.outflow_id, link.inflow_id) == (first.id, inn.id)
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.payload == {"outflow_id": second.id, "candidates": [inn.id]}


async def test_reconcile_keeps_the_older_of_two_equal_links(session: AsyncSession) -> None:
    """Two providers report one outflow, each observation rule-linked to a different
    inflow. Both links were saved at a nominal 9 tenths, the older one with float
    noise (0.8999999999999999); compared in tenths they tie and the older link stays."""
    bank = await make_account(session, source="simplefin")
    alias = await make_account(session, source="plaid", canonical_account_id=bank.id)
    savings = await make_account(session, source="simplefin", type=AccountType.savings)
    out = await make_txn(
        session, bank, amount_cents=-10000, posted_on=date(2026, 7, 1), description="TRANSFER OUT"
    )
    out_copy = await make_txn(
        session, alias, amount_cents=-10000, posted_on=date(2026, 7, 1), description="TRANSFER OUT"
    )
    first_in = await make_txn(
        session, savings, amount_cents=10000, posted_on=date(2026, 7, 1), description="TRANSFER IN"
    )
    second_in = await make_txn(
        session, savings, amount_cents=10000, posted_on=date(2026, 7, 1), description="TRANSFER IN"
    )
    session.add(
        TransferLink(
            outflow_id=out.id, inflow_id=first_in.id, confidence=0.8999999999999999, method="rule"
        )
    )
    session.add(
        TransferLink(outflow_id=out_copy.id, inflow_id=second_in.id, confidence=0.9, method="rule")
    )
    await session.commit()
    await reconcile_provider_overlap(session, TODAY)
    link = (await session.execute(select(TransferLink))).scalar_one()
    assert link.inflow_id == first_in.id


@pytest.mark.parametrize(
    "description",
    [
        "Zelle payment from JANE DOE 1234",
        "ZELLE PAYMENT TO JANE DOE",
        "zelle  payment   from jane doe",
        "VENMO PAYMENT 1234",
        "PayPal Inst Xfer JANE DOE",
    ],
)
def test_p2p_rail_wording_is_not_a_transfer_keyword(description: str) -> None:
    out = Transaction(posted_on=date(2026, 7, 1), description="Acme Wallet: Add Money")
    inn = Transaction(posted_on=date(2026, 7, 1), description=description)
    assert score_pair(out, inn, AccountType.credit) == PairScore(7, False)


def test_text_after_a_rail_still_counts() -> None:
    out = Transaction(posted_on=date(2026, 7, 1), description="ACME STORE")
    inn = Transaction(posted_on=date(2026, 7, 1), description="Zelle payment from JANE DOE ACH")
    assert score_pair(out, inn, AccountType.credit).keyword


@pytest.mark.parametrize(
    ("description", "keyword"),
    [
        ("CORNER CAFE ELMTON BEACH CA", False),
        ("buy 2 shares of Acme for $10.00 each", False),
        ("ACME COACH LINES", False),
        ("ACME BANK ACH PMT", True),
        ("ONETIMEPAYMENT", True),
        ("ACME PAYMENTS PPD", True),
    ],
)
def test_ach_must_be_a_whole_word(description: str, keyword: bool) -> None:
    """Only `ach` is matched as a whole word ("BEACH", "COACH" and "each" are not
    transfers); every other keyword stays a substring, so glued descriptors such as
    "ONETIMEPAYMENT" still count."""
    out = Transaction(posted_on=date(2026, 7, 1), description="ACME STORE")
    inn = Transaction(posted_on=date(2026, 7, 1), description=description)
    assert score_pair(out, inn, AccountType.credit).keyword is keyword


async def test_p2p_inflow_does_not_link_to_a_wallet_top_up(session: AsyncSession) -> None:
    """A friend's Zelle inflow and an unrelated same-amount card outflow on one day
    must not pair: the rail's own "payment" is not a transfer keyword."""
    card = await make_account(session, type=AccountType.credit)
    checking = await make_account(session, type=AccountType.checking)
    out = await make_txn(
        session,
        card,
        amount_cents=-30000,
        posted_on=date(2026, 7, 1),
        description="Acme Wallet: Add Money",
    )
    inn = await make_txn(
        session,
        checking,
        amount_cents=30000,
        posted_on=date(2026, 7, 1),
        description="Zelle payment from JANE DOE 1234",
    )
    stats = await link_transfers(session, llm=None)
    assert (stats.linked, stats.review) == (0, 1)
    assert (await session.execute(select(TransferLink))).scalar_one_or_none() is None
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.payload == {"outflow_id": out.id, "candidates": [inn.id]}


async def _wallet_and_p2p(session: AsyncSession) -> tuple[Transaction, Transaction]:
    """A card's wallet top-up and a friend's same-day, same-amount Zelle inflow."""
    card = await make_account(session, type=AccountType.credit)
    checking = await make_account(session, type=AccountType.checking)
    out = await make_txn(
        session,
        card,
        amount_cents=-30000,
        posted_on=date(2026, 7, 10),
        description="Acme Wallet: Add Money",
    )
    inn = await make_txn(
        session,
        checking,
        amount_cents=30000,
        posted_on=date(2026, 7, 10),
        description="Zelle payment from JANE DOE 1234",
    )
    return out, inn


async def test_stale_rule_link_is_dropped_and_asked_about(session: AsyncSession) -> None:
    out, inn = await _wallet_and_p2p(session)
    session.add(TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=0.9, method="rule"))
    await session.flush()
    stats = await link_transfers(session, llm=None)
    assert (stats.linked, stats.review, stats.unlinked) == (0, 1, 1)
    assert (await session.execute(select(TransferLink))).scalar_one_or_none() is None
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.payload == {"outflow_id": out.id, "candidates": [inn.id]}


async def test_qualifying_rule_link_stays_and_is_restamped_in_tenths(
    session: AsyncSession,
) -> None:
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
        posted_on=date(2026, 7, 1),
        description="TRANSFER FROM CHECKING",
    )
    session.add(
        TransferLink(
            outflow_id=out.id, inflow_id=inn.id, confidence=0.9999999999999999, method="rule"
        )
    )
    await session.flush()
    stats = await link_transfers(session, llm=None)
    assert (stats.linked, stats.review, stats.unlinked) == (0, 0, 0)
    link = (await session.execute(select(TransferLink))).scalar_one()
    assert (link.outflow_id, link.inflow_id, link.confidence) == (out.id, inn.id, 1.0)


@pytest.mark.parametrize(("method", "confidence"), [("manual", 1.0), ("llm", 0.9)])
async def test_human_and_llm_links_are_never_rescored(
    session: AsyncSession, method: str, confidence: float
) -> None:
    out, inn = await _wallet_and_p2p(session)
    session.add(
        TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=confidence, method=method)
    )
    await session.flush()
    stats = await link_transfers(session, llm=None)
    assert (stats.linked, stats.review, stats.unlinked) == (0, 0, 0)
    link = (await session.execute(select(TransferLink))).scalar_one()
    assert (link.outflow_id, link.inflow_id, link.method) == (out.id, inn.id, method)


async def test_freed_leg_re_pairs_in_the_same_run(session: AsyncSession) -> None:
    """Dropping the stale link frees the inflow for a genuine same-day transfer from
    savings, which links by rule in the same run; the wallet top-up loses that inflow
    to the stronger pair and is asked about."""
    out, inn = await _wallet_and_p2p(session)
    savings = await make_account(session, type=AccountType.savings)
    move = await make_txn(
        session,
        savings,
        amount_cents=-30000,
        posted_on=date(2026, 7, 10),
        description="TRANSFER TO CHECKING",
    )
    session.add(TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=0.9, method="rule"))
    await session.flush()
    stats = await link_transfers(session, llm=None)
    assert (stats.linked, stats.review, stats.unlinked) == (1, 1, 1)
    link = (await session.execute(select(TransferLink))).scalar_one()
    assert (link.outflow_id, link.inflow_id, link.method) == (move.id, inn.id, "rule")
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.payload == {"outflow_id": out.id, "candidates": [inn.id]}


async def test_rescore_judges_a_duplicate_observation_as_its_survivor(
    session: AsyncSession,
) -> None:
    """The stale link points at the second provider's copy of the Zelle inflow. The
    re-score follows it to the surviving observation, the one the linker scores."""
    card = await make_account(session, type=AccountType.credit, source="simplefin")
    checking = await make_account(session, source="simplefin")
    alias = await make_account(session, source="plaid", canonical_account_id=checking.id)
    out = await make_txn(
        session,
        card,
        amount_cents=-30000,
        posted_on=date(2026, 7, 10),
        description="Acme Wallet: Add Money",
    )
    inn = await make_txn(
        session,
        checking,
        amount_cents=30000,
        posted_on=date(2026, 7, 10),
        description="Zelle payment from JANE DOE 1234",
    )
    copy = await make_txn(
        session,
        alias,
        amount_cents=30000,
        posted_on=date(2026, 7, 10),
        description="ZELLE PAYMENT FROM JANE DOE",
        duplicate_of_id=inn.id,
    )
    session.add(TransferLink(outflow_id=out.id, inflow_id=copy.id, confidence=0.9, method="rule"))
    await session.flush()
    stats = await link_transfers(session, llm=None)
    assert (stats.linked, stats.review, stats.unlinked) == (0, 1, 1)
    assert (await session.execute(select(TransferLink))).scalar_one_or_none() is None
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert item.payload == {"outflow_id": out.id, "candidates": [inn.id]}


async def test_sync_drops_a_stale_link_before_spend_is_counted(session: AsyncSession) -> None:
    """In a full sync the re-score runs inside the transfers stage, so the freed
    wallet top-up counts as spend in the same sync's numbers."""
    out, inn = await _wallet_and_p2p(session)
    session.add(TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=0.9, method="rule"))
    await session.commit()
    report = await run_sync(session, [RecordingAdapter()], llm=None, today=TODAY)
    assert (report.transfers.unlinked, report.transfers.linked) == (1, 0)
    assert (await session.execute(select(TransferLink))).scalar_one_or_none() is None
    assert (await power_report(session, TODAY)).spent_so_far_cents == 30000
    item = (await session.execute(select(ReviewItem))).scalar_one()
    assert (item.kind, item.status) == ("transfer_pair", "open")
