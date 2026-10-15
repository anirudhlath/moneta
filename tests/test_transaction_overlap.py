"""Cross-provider reconciliation against real isolated SQLite observations."""

from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.logical import load_logical_data
from moneta.models import (
    Account,
    AliasSource,
    LinkMethod,
    MerchantAlias,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
    SeriesStatus,
    Transaction,
    TransferLink,
)
from tests.factories import make_account, make_series, make_txn

TODAY = date(2026, 7, 10)


async def reconcile(session: AsyncSession) -> Any:
    from moneta.pipelines.reconcile import reconcile_provider_overlap

    return await reconcile_provider_overlap(session, TODAY)


async def accounts(session: AsyncSession, reverse: bool = False) -> tuple[Account, Account]:
    first, second = ("simplefin", "plaid") if reverse else ("plaid", "simplefin")
    a = await make_account(session, source=first)
    b = await make_account(session, source=second, canonical_account_id=a.id)
    return (b, a) if reverse else (a, b)


async def fingerprint(session: AsyncSession) -> list[tuple[Any, ...]]:
    rows = (
        await session.execute(
            select(
                Transaction.id,
                Transaction.account_id,
                Transaction.aggregator_id,
                Transaction.posted_on,
                Transaction.amount_cents,
                Transaction.description,
                Transaction.raw,
            ).order_by(Transaction.id)
        )
    ).all()
    return [tuple(row) for row in rows]


async def questions(session: AsyncSession) -> list[ReviewItem]:
    return list(
        await session.scalars(
            select(ReviewItem).where(
                ReviewItem.kind == ReviewKind.transaction_overlap,
                ReviewItem.status == ReviewStatus.open,
            )
        )
    )


async def test_union_counts_overlap_once_and_retains_history(session: AsyncSession) -> None:
    plaid, simplefin = await accounts(session)
    old = await make_txn(session, plaid, posted_on=date(2024, 1, 1), description="Old Shop")
    a = await make_txn(session, plaid, description="Coffee House", raw={"provider": "plaid"})
    b = await make_txn(
        session, simplefin, description="Coffee House", raw={"provider": "simplefin"}
    )
    unique = await make_txn(session, simplefin, description="Book Shop")
    before = await fingerprint(session)
    stats = await reconcile(session)
    logical = await load_logical_data(session)
    assert {t.id for t in logical.transactions} == {old.id, a.id, unique.id}
    assert b.duplicate_of_id == a.id
    assert stats.transactions_suppressed == 1
    assert await fingerprint(session) == before
    assert len(before) == 4
    again = await reconcile(session)
    assert again.transactions_suppressed == again.transactions_restored == 0
    assert again.reviews_opened == again.reviews_superseded == 0
    assert (await session.execute(text("PRAGMA foreign_key_check"))).all() == []


@pytest.mark.parametrize("right_count,want", [(2, 2), (3, 3)])
async def test_equal_purchases_preserve_source_multiplicity(
    session: AsyncSession,
    right_count: int,
    want: int,
) -> None:
    a, b = await accounts(session)
    for account, count in [(a, 2), (b, right_count)]:
        for _ in range(count):
            await make_txn(session, account, description="Coffee House")
    await reconcile(session)
    assert len((await load_logical_data(session)).transactions) == want


@pytest.mark.parametrize("days,want", [(0, 1), (4, 1), (5, 2)])
async def test_unique_named_counterpart_bounds_date_offset(
    session: AsyncSession,
    days: int,
    want: int,
) -> None:
    a, b = await accounts(session)
    await make_txn(session, a, description="Coffee House", merchant="Coffee House")
    await make_txn(
        session,
        b,
        description="SQ *Coffee House",
        merchant="Coffee House",
        posted_on=date(2026, 7, 1) + timedelta(days=days),
    )
    await reconcile(session)
    assert len((await load_logical_data(session)).transactions) == want


@pytest.mark.parametrize("change", ["merchant", "sign", "account", "provider", "generic_offset"])
async def test_incompatible_observations_stay_distinct(session: AsyncSession, change: str) -> None:
    a, b = await accounts(session)
    kwargs: dict[str, Any] = {"description": "Coffee House", "merchant": "Coffee House"}
    if change == "merchant":
        kwargs.update(description="Book Shop", merchant="Book Shop")
    elif change == "sign":
        kwargs["amount_cents"] = 1000
    elif change == "account":
        b.canonical_account_id = None
        b.org_name = "Different Bank"
    elif change == "provider":
        b.source = a.source
    elif change == "generic_offset":
        kwargs.update(description="PAYMENT", merchant="Payment", posted_on=date(2026, 7, 3))
    await make_txn(
        session,
        a,
        description="PAYMENT" if change == "generic_offset" else "Coffee House",
        merchant="Payment" if change == "generic_offset" else "Coffee House",
    )
    await make_txn(session, b, **kwargs)
    await reconcile(session)
    assert len((await load_logical_data(session)).transactions) == 2


async def test_competing_offsets_open_one_durable_question_per_observation(
    session: AsyncSession,
) -> None:
    a, b = await accounts(session)
    left = await make_txn(session, a, description="Coffee House", merchant="Coffee House")
    rights = [
        await make_txn(
            session,
            b,
            description="Coffee House",
            merchant="Coffee House",
            posted_on=date(2026, 7, d),
        )
        for d in (2, 3)
    ]
    await reconcile(session)
    assert len((await load_logical_data(session)).transactions) == 3
    items = await questions(session)
    item = next(i for i in items if i.payload["transaction_id"] == left.id)
    assert item.payload["candidates"] == [t.id for t in rights]
    ids = {i.id for i in items}
    again = await reconcile(session)
    assert {i.id for i in await questions(session)} == ids
    assert again.reviews_opened == 0


@pytest.mark.parametrize("reverse", [False, True])
async def test_survivor_prefers_root_provider_across_arrival_order(
    session: AsyncSession, reverse: bool
) -> None:
    a, b = await accounts(session, reverse)
    simplefin = await make_txn(session, b, description="Coffee House")
    plaid = await make_txn(session, a, description="Coffee House")
    await reconcile(session)
    expected = simplefin if reverse else plaid
    assert [t.id for t in (await load_logical_data(session)).transactions] == [expected.id]


async def test_reviewed_merchant_observation_survives(session: AsyncSession) -> None:
    a, b = await accounts(session)
    left = await make_txn(session, a, description="Coffee House", merchant="Coffee House")
    right = await make_txn(session, b, description="SQ *Coffee House", merchant="Cafe")
    session.add(
        MerchantAlias(raw_descriptor=right.description, merchant="Cafe", source=AliasSource.manual)
    )
    await reconcile(session)
    assert left.duplicate_of_id == right.id
    assert right.merchant == "Cafe"


@pytest.mark.parametrize("kind", ["merchant", "recurring", "transfer"])
async def test_reviewed_classification_conflict_requires_review(
    session: AsyncSession, kind: str
) -> None:
    a, b = await accounts(session)
    left = await make_txn(session, a, description="Coffee House", merchant="Coffee House")
    right = await make_txn(session, b, description="SQ *Coffee House", merchant="Cafe")
    if kind == "merchant":
        for txn in (left, right):
            session.add(
                MerchantAlias(
                    raw_descriptor=txn.description, merchant=txn.merchant, source=AliasSource.manual
                )
            )
    elif kind == "recurring":
        for txn, answer in [(left, True), (right, False)]:
            session.add(
                ReviewItem(
                    kind=ReviewKind.recurring_cluster,
                    question="Recurring?",
                    payload={"merchant": txn.merchant, "direction": "outflow"},
                    status=ReviewStatus.resolved,
                    resolution={"is_recurring": answer, "resolved_by": "manual"},
                )
            )
    else:
        for txn, transfer_answer in [(left, None), (right, 1234)]:
            session.add(
                ReviewItem(
                    kind=ReviewKind.transfer_pair,
                    question="Transfer?",
                    payload={"outflow_id": txn.id, "candidates": []},
                    status=ReviewStatus.resolved,
                    resolution={"inflow_id": transfer_answer, "resolved_by": "manual"},
                )
            )
    await reconcile(session)
    assert left.duplicate_of_id is right.duplicate_of_id is None
    assert await questions(session)


async def test_upstream_correction_restores_observation_and_retires_invalid_link(
    session: AsyncSession,
) -> None:
    a, b = await accounts(session)
    left = await make_txn(session, a, description="Coffee House")
    right = await make_txn(session, b, description="Coffee House")
    other = await make_account(session, source="plaid", org_name="Other Bank")
    inflow = await make_txn(session, other, amount_cents=1000)
    session.add(
        TransferLink(
            outflow_id=left.id, inflow_id=inflow.id, confidence=0.9, method=LinkMethod.rule
        )
    )
    await reconcile(session)
    left.amount_cents = -2000
    before = await fingerprint(session)
    stats = await reconcile(session)
    assert stats.transactions_restored == 1
    assert right.duplicate_of_id is None
    assert list(await session.scalars(select(TransferLink))) == []
    assert await fingerprint(session) == before


async def test_transfer_endpoints_preserve_reviewed_survivors(session: AsyncSession) -> None:
    a, b = await accounts(session)
    c = await make_account(session, source="plaid", org_name="Other Bank")
    d = await make_account(
        session, source="simplefin", org_name="Other Bank", canonical_account_id=c.id
    )
    outs = [await make_txn(session, account, description="Card payment") for account in (a, b)]
    inns = [
        await make_txn(session, account, amount_cents=1000, description="Card payment")
        for account in (c, d)
    ]
    session.add(
        TransferLink(
            outflow_id=outs[0].id, inflow_id=inns[0].id, confidence=0.9, method=LinkMethod.rule
        )
    )
    reviewed = TransferLink(
        outflow_id=outs[1].id, inflow_id=inns[1].id, confidence=1, method=LinkMethod.manual
    )
    session.add(reviewed)
    before = await fingerprint(session)
    await reconcile(session)
    links = list(await session.scalars(select(TransferLink)))
    assert links == [reviewed]
    assert (reviewed.outflow_id, reviewed.inflow_id) == (outs[1].id, inns[1].id)
    assert outs[0].duplicate_of_id == outs[1].id
    assert inns[0].duplicate_of_id == inns[1].id
    assert await fingerprint(session) == before
    await reconcile(session)
    assert list(await session.scalars(select(TransferLink))) == [reviewed]


async def test_suppressed_questions_are_explicitly_superseded(session: AsyncSession) -> None:
    a, b = await accounts(session)
    await make_txn(session, a, description="Coffee House", merchant="Coffee House")
    series = await make_series(session, merchant="Cafe")
    duplicate = await make_txn(
        session, b, description="SQ *Coffee House", merchant="Cafe", series_id=series.id
    )
    items = [
        ReviewItem(
            kind=ReviewKind.merchant,
            question="Merchant?",
            payload={"descriptor": duplicate.description},
        ),
        ReviewItem(
            kind=ReviewKind.recurring_cluster,
            question="Recurring?",
            payload={"merchant": "Cafe", "direction": "outflow"},
        ),
        ReviewItem(
            kind=ReviewKind.price_change, question="Price?", payload={"series_id": series.id}
        ),
        ReviewItem(
            kind=ReviewKind.transfer_pair,
            question="Transfer?",
            payload={"outflow_id": duplicate.id, "candidates": []},
        ),
    ]
    history = ReviewItem(
        kind=ReviewKind.price_change,
        question="Old price?",
        payload={"series_id": series.id},
        status=ReviewStatus.resolved,
        resolution={"is_price_change": False, "resolved_by": "manual"},
    )
    session.add_all([*items, history])
    before = await fingerprint(session)
    stats = await reconcile(session)
    assert stats.reviews_superseded == 4
    for item in items:
        assert item.status == ReviewStatus.resolved
        assert item.resolution is not None and item.resolution["superseded_reason"]
        assert item.resolution["resolved_by"] == "reconciliation"
    assert history.resolution == {"is_price_change": False, "resolved_by": "manual"}
    assert series.status == SeriesStatus.ended
    assert duplicate.series_id == series.id
    assert await fingerprint(session) == before


async def test_manual_rejection_and_choice_are_durable(session: AsyncSession) -> None:
    from moneta.pipelines.transaction_overlap import set_transaction_duplicate

    a, b = await accounts(session)
    left = await make_txn(session, a, description="Coffee House")
    right = await make_txn(session, b, description="Coffee House")
    await reconcile(session)
    await set_transaction_duplicate(session, right.id, None)
    await reconcile(session)
    assert right.duplicate_of_id is None
    await set_transaction_duplicate(session, left.id, right.id)
    await reconcile(session)
    assert left.duplicate_of_id == right.id
    assert right.duplicate_of_id is None


@pytest.mark.parametrize(
    "invalid", ["self", "missing", "cross_account", "same_provider", "amount", "cycle"]
)
async def test_manual_choice_validates_before_mutation(session: AsyncSession, invalid: str) -> None:
    from moneta.pipelines.transaction_overlap import set_transaction_duplicate

    a, b = await accounts(session)
    left = await make_txn(session, a, description="Coffee House")
    right = await make_txn(session, b, description="Coffee House")
    target = right.id
    if invalid == "self":
        target = left.id
    elif invalid == "missing":
        target = 99999
    elif invalid == "cross_account":
        b.canonical_account_id = None
    elif invalid == "same_provider":
        b.source = a.source
    elif invalid == "amount":
        right.amount_cents = -999
    elif invalid == "cycle":
        right.duplicate_of_id = left.id
    await session.flush()
    with pytest.raises(ValueError):
        await set_transaction_duplicate(session, left.id, target)
    assert left.duplicate_of_id is None
    assert list(await session.scalars(select(ReviewItem))) == []


async def test_exact_matches_take_precedence_over_date_offsets(session: AsyncSession) -> None:
    a, b = await accounts(session)
    exact = await make_txn(session, a, description="Coffee House", merchant="Coffee House")
    earlier = await make_txn(
        session, a, description="Coffee House", merchant="Coffee House", posted_on=date(2026, 6, 30)
    )
    right = await make_txn(session, b, description="Coffee House", merchant="Coffee House")
    await reconcile(session)
    assert right.duplicate_of_id == exact.id
    assert earlier.duplicate_of_id is None
    assert await questions(session) == []


async def test_reviewed_transfer_choices_to_distinct_occurrences_conflict(
    session: AsyncSession,
) -> None:
    a, b = await accounts(session)
    other = await make_account(session, source="plaid", org_name="Other Bank")
    left = await make_txn(session, a, description="Card payment")
    right = await make_txn(session, b, description="Card payment")
    for out, day in [(left, 1), (right, 3)]:
        inn = await make_txn(session, other, amount_cents=1000, posted_on=date(2026, 7, day))
        session.add(
            TransferLink(
                outflow_id=out.id, inflow_id=inn.id, method=LinkMethod.manual, confidence=1
            )
        )
    await reconcile(session)
    assert left.duplicate_of_id is right.duplicate_of_id is None
    assert len(list(await session.scalars(select(TransferLink)))) == 2
    assert await questions(session)


async def test_rejected_pair_does_not_hide_a_new_candidate(session: AsyncSession) -> None:
    from moneta.pipelines.transaction_overlap import set_transaction_duplicate

    a, b = await accounts(session)
    left = await make_txn(session, a, description="Coffee House")
    right = await make_txn(session, b, description="Coffee House")
    await set_transaction_duplicate(session, right.id, None)
    right.posted_on = date(2026, 7, 2)
    later = await make_txn(session, a, description="Coffee House", posted_on=date(2026, 7, 2))
    await reconcile(session)
    assert right.duplicate_of_id == later.id
    assert left.duplicate_of_id is None


async def test_account_unlink_restores_duplicates(session: AsyncSession) -> None:
    from moneta.pipelines.account_overlap import set_account_link

    a, b = await accounts(session)
    left = await make_txn(session, a, description="Coffee House")
    right = await make_txn(session, b, description="Coffee House")
    await reconcile(session)
    await set_account_link(session, b.id, None)
    stats = await reconcile(session)
    assert stats.transactions_restored == 1
    assert left.duplicate_of_id is right.duplicate_of_id is None


async def test_changed_description_retires_stale_overlap_questions(session: AsyncSession) -> None:
    a, b = await accounts(session)
    left = await make_txn(session, a, description="Coffee House", merchant="Coffee House")
    rights = [
        await make_txn(
            session,
            b,
            description="Coffee House",
            merchant="Coffee House",
            posted_on=date(2026, 7, d),
        )
        for d in (2, 3)
    ]
    await reconcile(session)
    old = await questions(session)
    for right in rights:
        right.description, right.merchant = "Book Shop", "Book Shop"
    stats = await reconcile(session)
    assert left.duplicate_of_id is None
    assert stats.reviews_superseded == len(old)
    assert await questions(session) == []
    assert all(i.resolution and i.resolution.get("superseded_reason") for i in old)


async def test_transfer_review_repoints_to_survivor_and_deduplicates_candidates(
    session: AsyncSession,
) -> None:
    a, b = await accounts(session)
    c = await make_account(session, source="plaid", org_name="Other Bank")
    d = await make_account(
        session, source="simplefin", org_name="Other Bank", canonical_account_id=c.id
    )
    out = await make_txn(session, a, description="Card payment")
    duplicate = await make_txn(session, b, description="Card payment")
    inn = await make_txn(session, c, amount_cents=1000)
    other_in = await make_txn(session, d, amount_cents=1000)
    item = ReviewItem(
        kind=ReviewKind.transfer_pair,
        question="Transfer?",
        payload={"outflow_id": duplicate.id, "candidates": [inn.id, other_in.id]},
    )
    session.add(item)
    await reconcile(session)
    assert item.status == ReviewStatus.open
    assert item.payload == {"outflow_id": out.id, "candidates": [inn.id]}


async def test_manual_mutator_rolls_back_without_own_commit(session: AsyncSession) -> None:
    from moneta.pipelines.transaction_overlap import set_transaction_duplicate

    a, b = await accounts(session)
    left = await make_txn(session, a, description="Coffee House")
    right = await make_txn(session, b, description="Coffee House")
    left_id, right_id = left.id, right.id
    await session.commit()
    await set_transaction_duplicate(session, right_id, left_id)
    await session.rollback()
    await session.refresh(right)
    assert right.duplicate_of_id is None
    assert list(await session.scalars(select(ReviewItem))) == []


async def test_manual_identity_is_revalidated_after_descriptor_correction(
    session: AsyncSession,
) -> None:
    from moneta.pipelines.transaction_overlap import set_transaction_duplicate

    a, b = await accounts(session)
    left = await make_txn(session, a, description="Coffee House")
    right = await make_txn(session, b, description="Cafe", merchant="Cafe")
    await set_transaction_duplicate(session, right.id, left.id)
    await reconcile(session)
    assert right.duplicate_of_id == left.id
    right.description, right.merchant = "Book Shop", "Book Shop"
    stats = await reconcile(session)
    assert right.duplicate_of_id is None
    assert stats.transactions_restored == 1
    historical = list(
        await session.scalars(
            select(ReviewItem).where(ReviewItem.kind == ReviewKind.transaction_overlap)
        )
    )
    assert any(i.resolution and i.resolution.get("duplicate_of_id") == left.id for i in historical)


async def test_financial_account_and_holding_observations_are_unchanged(
    session: AsyncSession,
) -> None:
    from moneta.models import Holding

    a, b = await accounts(session)
    a.balance_cents, b.balance_cents = 100001, 98765
    a.balance_date, b.balance_date = date(2026, 7, 1), date(2026, 7, 2)
    for account, value in [(a, 15000), (b, 16000)]:
        session.add(
            Holding(account_id=account.id, symbol="TEST", quantity=1.25, market_value_cents=value)
        )
        await make_txn(session, account, description="Coffee House")
    account_query = select(
        Account.id,
        Account.aggregator_id,
        Account.source,
        Account.name,
        Account.balance_cents,
        Account.balance_date,
        Account.currency,
    ).order_by(Account.id)
    holding_query = select(
        Holding.id,
        Holding.account_id,
        Holding.symbol,
        Holding.quantity,
        Holding.market_value_cents,
        Holding.vested_quantity,
        Holding.unvested_quantity,
    ).order_by(Holding.id)
    before_accounts = (await session.execute(account_query)).all()
    before_holdings = (await session.execute(holding_query)).all()
    before_transactions = await fingerprint(session)
    await reconcile(session)
    assert (await session.execute(account_query)).all() == before_accounts
    assert (await session.execute(holding_query)).all() == before_holdings
    assert await fingerprint(session) == before_transactions


async def test_generic_merchant_label_does_not_merge_distinct_named_descriptors(
    session: AsyncSession,
) -> None:
    a, b = await accounts(session)
    await make_txn(session, a, description="Coffee House", merchant="Payment")
    await make_txn(session, b, description="Book Shop", merchant="Payment")
    await reconcile(session)
    assert len((await load_logical_data(session)).transactions) == 2


@pytest.mark.parametrize("root_source", ["plaid", "simplefin"])
async def test_both_reviewed_root_wins_regardless_of_classification_count(
    session: AsyncSession,
    root_source: str,
) -> None:
    root = await make_account(session, source=root_source)
    alias = await make_account(
        session,
        source="simplefin" if root_source == "plaid" else "plaid",
        canonical_account_id=root.id,
    )
    root_txn = await make_txn(session, root, description="Coffee House", merchant="Coffee House")
    alias_txn = await make_txn(
        session, alias, description="SQ *Coffee House", merchant="Coffee House"
    )
    session.add(
        ReviewItem(
            kind=ReviewKind.recurring_cluster,
            question="Recurring?",
            payload={"merchant": "Coffee House", "direction": "outflow"},
            status=ReviewStatus.resolved,
            resolution={"is_recurring": True, "resolved_by": "manual"},
        )
    )
    session.add(
        MerchantAlias(
            raw_descriptor=alias_txn.description, merchant="Coffee House", source=AliasSource.manual
        )
    )
    await reconcile(session)
    assert alias_txn.duplicate_of_id == root_txn.id
    assert root_txn.duplicate_of_id is None


async def test_row_id_precedes_provider_transaction_id_for_repeated_matches(
    session: AsyncSession,
) -> None:
    root, alias = await accounts(session)
    first = await make_txn(
        session, root, description="Coffee House", aggregator_id="z-last-lexically"
    )
    second = await make_txn(
        session, root, description="Coffee House", aggregator_id="a-first-lexically"
    )
    duplicate = await make_txn(session, alias, description="Coffee House")
    await reconcile(session)
    assert duplicate.duplicate_of_id == first.id
    assert second.duplicate_of_id is None


async def test_row_id_breaks_tie_when_neither_observation_uses_root_provider(
    session: AsyncSession,
) -> None:
    root = await make_account(session, source="root-provider")
    sf = await make_account(session, source="simplefin", canonical_account_id=root.id)
    plaid = await make_account(session, source="plaid", canonical_account_id=root.id)
    first = await make_txn(session, sf, description="Coffee House", aggregator_id="z")
    second = await make_txn(session, plaid, description="Coffee House", aggregator_id="a")
    await reconcile(session)
    assert second.duplicate_of_id == first.id


@pytest.mark.parametrize("ambiguity", ["rejected", "competing"])
async def test_reviewed_transfers_require_same_endpoint_occurrence(
    session: AsyncSession,
    ambiguity: str,
) -> None:
    from moneta.pipelines.transaction_overlap import set_transaction_duplicate

    a, b = await accounts(session)
    c = await make_account(session, source="plaid", org_name="Other Bank")
    d = await make_account(
        session, source="simplefin", org_name="Other Bank", canonical_account_id=c.id
    )
    outs = [await make_txn(session, account, description="Card payment") for account in (a, b)]
    inns = [
        await make_txn(session, account, amount_cents=1000, description="Card payment")
        for account in (c, d)
    ]
    if ambiguity == "rejected":
        await set_transaction_duplicate(session, inns[1].id, None)
    else:
        await make_txn(session, d, amount_cents=1000, description="Card payment")
    links = [
        TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1, method=LinkMethod.manual)
        for out, inn in zip(outs, inns, strict=True)
    ]
    session.add_all(links)
    before = await fingerprint(session)
    await reconcile(session)
    assert outs[0].duplicate_of_id is outs[1].duplicate_of_id is None
    assert set(await session.scalars(select(TransferLink))) == set(links)
    assert [(link.outflow_id, link.inflow_id) for link in links] == [
        (outs[0].id, inns[0].id),
        (outs[1].id, inns[1].id),
    ]
    assert await questions(session)
    assert await fingerprint(session) == before
    if ambiguity == "rejected":
        assert inns[0].duplicate_of_id is inns[1].duplicate_of_id is None
    again = await reconcile(session)
    assert again.transactions_suppressed == again.transactions_restored == 0
    assert set(await session.scalars(select(TransferLink))) == set(links)


@pytest.mark.parametrize("rejected_source", ["plaid", "simplefin"])
async def test_manual_no_transfer_survives_alias_rule_link(
    session: AsyncSession,
    rejected_source: str,
) -> None:
    a, b = await accounts(session)
    outs = [await make_txn(session, account, description="Card payment") for account in (a, b)]
    rejected, alias = outs if rejected_source == "plaid" else list(reversed(outs))
    other = await make_account(session, source="plaid", org_name="Other Bank")
    inn = await make_txn(session, other, description="Card payment", amount_cents=1000)
    decision = ReviewItem(
        kind=ReviewKind.transfer_pair,
        question="Transfer?",
        payload={"outflow_id": rejected.id, "candidates": [inn.id]},
        status=ReviewStatus.resolved,
        resolution={"inflow_id": None, "resolved_by": "manual"},
    )
    session.add_all(
        [
            decision,
            TransferLink(
                outflow_id=alias.id, inflow_id=inn.id, confidence=0.9, method=LinkMethod.rule
            ),
        ]
    )
    before = await fingerprint(session)
    await reconcile(session)
    assert alias.duplicate_of_id == rejected.id
    assert list(await session.scalars(select(TransferLink))) == []
    assert decision.resolution == {"inflow_id": None, "resolved_by": "manual"}
    assert await fingerprint(session) == before
    again = await reconcile(session)
    assert again.transactions_suppressed == again.transactions_restored == 0
    assert list(await session.scalars(select(TransferLink))) == []


async def test_merchant_question_without_its_descriptor_is_superseded(
    session: AsyncSession,
) -> None:
    # an upstream correction rewrote the descriptor: no transaction carries the asked text
    a, _ = await accounts(session)
    await make_txn(session, a, description="ACME TOLL 12A34B 877-555-0100 TX", merchant="Acme")
    item = ReviewItem(
        kind=ReviewKind.merchant,
        question="Merchant?",
        payload={"descriptor": "ACME TOLL 12A34B", "fallback": "Acme Toll 12A34B"},
    )
    session.add(item)
    stats = await reconcile(session)
    assert stats.reviews_superseded == 1
    assert item.status == ReviewStatus.resolved
    assert item.resolution is not None
    assert item.resolution["superseded_reason"] == "Descriptor no longer appears on any transaction"


async def test_merchant_question_with_clean_names_is_superseded(session: AsyncSession) -> None:
    # the rules learned to clean this descriptor after the question opened
    a, _ = await accounts(session)
    await make_txn(session, a, description="ACME ROOM 1234567890123-45", merchant="Acme Room")
    clean = ReviewItem(
        kind=ReviewKind.merchant,
        question="Merchant?",
        payload={"descriptor": "ACME ROOM 1234567890123-45", "fallback": "Acme Room -45"},
    )
    await make_txn(session, a, description="WITHDRAWAL 03/14", merchant="Withdrawal 03/14")
    still_unclear = ReviewItem(
        kind=ReviewKind.merchant,
        question="Merchant?",
        payload={"descriptor": "WITHDRAWAL 03/14", "fallback": "Withdrawal 03/14"},
    )
    session.add_all([clean, still_unclear])
    stats = await reconcile(session)
    assert stats.reviews_superseded == 1
    assert clean.status == ReviewStatus.resolved
    assert still_unclear.status == ReviewStatus.open


async def test_price_question_whose_transaction_is_gone_is_superseded(
    session: AsyncSession,
) -> None:
    a, _ = await accounts(session)
    series = await make_series(session, merchant="Acme Fitness", expected_cents=-12000)
    live = await make_txn(
        session,
        a,
        description="ACME FITNESS",
        merchant="Acme Fitness",
        amount_cents=-13000,
        series_id=series.id,
    )

    def question(transaction_id: int) -> ReviewItem:
        return ReviewItem(
            kind=ReviewKind.price_change,
            question="Did 'Acme Fitness' change price from $120.00 to $130.00?",
            payload={
                "series_id": series.id,
                "currency": "USD",
                "transaction_id": transaction_id,
                "merchant": "Acme Fitness",
                "old_cents": -12000,
                "new_cents": -13000,
                "occurred_on": "2026-07-01",
                "llm_flagged": True,
            },
        )

    stale, current = question(live.id + 1000), question(live.id)
    session.add_all([stale, current])
    stats = await reconcile(session)
    reason = "Transaction is no longer reported by its provider"
    assert stats.reviews_superseded == 1
    assert stale.status == ReviewStatus.resolved
    assert stale.resolution is not None
    assert stale.resolution["superseded_reason"] == reason
    assert current.status == ReviewStatus.open
