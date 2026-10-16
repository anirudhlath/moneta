"""Final-review regressions: effective decisions, not merely retained JSON."""

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.logical import load_logical_data
from moneta.models import (
    AccountType,
    AliasSource,
    Holding,
    MerchantAlias,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
    Transaction,
    TransferLink,
)
from moneta.pipelines.account_overlap import set_account_link
from moneta.pipelines.reconcile import rebuild_logical_derived, reconcile_provider_overlap
from moneta.pipelines.review import apply_resolution
from moneta.pipelines.run import run_sync
from moneta.views.cashflow import accrual_inflows, accrual_spend
from moneta.views.financing import compute_obligations
from moneta.views.networth import net_worth_report
from tests.conftest import RecordingAdapter
from tests.factories import make_account, make_txn
from tests.test_autoreview import ScriptedLLM

TODAY = date(2026, 7, 15)


@pytest.mark.parametrize("human_on_alias", [False, True])
@pytest.mark.parametrize("accepts", [False, True])
async def test_full_sync_pending_machine_question_cannot_overrule_human(
    session: AsyncSession, human_on_alias: bool, accepts: bool
) -> None:
    root = await make_account(session, source="simplefin")
    alias = await make_account(session, source="plaid", canonical_account_id=root.id)
    target = await make_account(session, source="simplefin", org_name="Other Bank")
    out = await make_txn(session, root, description="Coffee House")
    copy = await make_txn(session, alias, description="SQ *Coffee House")
    inn = await make_txn(session, target, amount_cents=1000, description="Receipt")
    alternative = await make_txn(session, target, amount_cents=1000, description="Other Receipt")
    human, machine = (copy, out) if human_on_alias else (out, copy)
    history = ReviewItem(
        kind=ReviewKind.transfer_pair,
        question="Human decision",
        payload={"outflow_id": human.id, "candidates": [inn.id]},
        status=ReviewStatus.resolved,
        resolution={"inflow_id": inn.id if accepts else None, "resolved_by": "manual"},
    )
    pending = ReviewItem(
        kind=ReviewKind.transfer_pair,
        question="Weaker pending question",
        payload={"outflow_id": machine.id, "candidates": [alternative.id]},
    )
    session.add_all([history, pending])
    await session.commit()
    original = (history.payload.copy(), dict(history.resolution or {}))
    llm = ScriptedLLM({"auto-resolving": {"inflow_id": alternative.id, "confident": True}})
    for _ in range(2):
        await run_sync(
            session, [RecordingAdapter("plaid"), RecordingAdapter("simplefin")], llm, TODAY
        )
        logical = await load_logical_data(session)
        survivor = logical.transaction_roots[out.id]
        assert logical.transaction_roots[copy.id] == survivor
        links = list(await session.scalars(select(TransferLink)))
        assert [(x.outflow_id, x.inflow_id, x.method) for x in links] == (
            [(survivor, inn.id, "manual")] if accepts else []
        )
        assert await accrual_spend(session, date(2026, 7, 1), TODAY) == (0 if accepts else 1000)
        assert (history.payload, history.resolution) == original
        assert pending.status == ReviewStatus.resolved
        assert (pending.resolution or {}).get("superseded_reason")


@pytest.mark.parametrize("accepts", [False, True])
@pytest.mark.parametrize("root_source", ["simplefin", "plaid"])
@pytest.mark.parametrize("through_sync", [False, True])
async def test_automatic_resolution_boundary_preserves_human_but_allows_new_human(
    session: AsyncSession, accepts: bool, root_source: str, through_sync: bool
) -> None:
    root = await make_account(session, source=root_source)
    alias = await make_account(
        session,
        source="plaid" if root_source == "simplefin" else "simplefin",
        canonical_account_id=root.id,
    )
    target = await make_account(session, org_name="Other Bank")
    out = await make_txn(session, root)
    copy = await make_txn(session, alias, duplicate_of_id=out.id)
    inn = await make_txn(session, target, amount_cents=1000)
    first = ReviewItem(
        kind=ReviewKind.transfer_pair,
        question="Human",
        payload={"outflow_id": copy.id, "candidates": [inn.id]},
    )
    session.add(first)
    await apply_resolution(session, first, {"inflow_id": inn.id if accepts else None})
    await session.commit()
    second = ReviewItem(
        kind=ReviewKind.transfer_pair,
        question="New",
        payload={"outflow_id": out.id, "candidates": [inn.id]},
    )
    session.add(second)
    await session.flush()
    with pytest.raises(ValueError, match="human"):
        await apply_resolution(
            session, second, {"inflow_id": None if accepts else inn.id}, resolved_by="llm"
        )
    await apply_resolution(session, second, {"inflow_id": None if accepts else inn.id})
    await session.commit()
    originals = [(i.payload.copy(), dict(i.resolution or {})) for i in [first, second]]
    raw = [
        (
            t.id,
            t.account_id,
            t.aggregator_id,
            t.posted_on,
            t.amount_cents,
            t.description,
            t.raw.copy(),
        )
        for t in [out, copy, inn]
    ]
    assert second.payload["transfer_review_ids"] == [first.id]
    for _ in range(2):
        if through_sync:
            await run_sync(
                session, [RecordingAdapter("plaid"), RecordingAdapter("simplefin")], None, TODAY
            )
        else:
            await reconcile_provider_overlap(session, TODAY)
            await rebuild_logical_derived(session, None, TODAY)
        logical = await load_logical_data(session)
        assert [t.id for t in logical.transactions if t.amount_cents < 0] == [out.id]
        assert [t.id for t in logical.transactions if t.amount_cents > 0] == [inn.id]
        assert logical.transaction_roots[copy.id] == out.id
        assert [
            (link.outflow_id, link.inflow_id, link.method)
            for link in await session.scalars(select(TransferLink))
        ] == ([] if accepts else [(out.id, inn.id, "manual")])
        assert await accrual_spend(session, date(2026, 7, 1), TODAY) == (1000 if accepts else 0)
        assert await accrual_inflows(session, date(2026, 7, 1), TODAY) == (1000 if accepts else 0)
        assert [(i.payload, i.resolution) for i in [first, second]] == originals
        assert [
            (t.id, t.account_id, t.aggregator_id, t.posted_on, t.amount_cents, t.description, t.raw)
            for t in [out, copy, inn]
        ] == raw
    assert first.resolution == {"inflow_id": inn.id if accepts else None, "resolved_by": "manual"}


@pytest.mark.parametrize("root_source", ["simplefin", "plaid"])
async def test_adjudication_does_not_resurrect_older_physical_answer_during_matching(
    session: AsyncSession, root_source: str
) -> None:
    from moneta.pipelines.transfers import effective_transfer_choices

    root = await make_account(session, source=root_source)
    alias = await make_account(
        session,
        source="plaid" if root_source == "simplefin" else "simplefin",
        canonical_account_id=root.id,
    )
    target = await make_account(session, org_name="Other Bank")
    out = await make_txn(session, root, description="Coffee House")
    copy = await make_txn(session, alias, description="SQ *Coffee House", duplicate_of_id=out.id)
    inn = await make_txn(session, target, amount_cents=1000)
    history = []
    for answer in (inn.id, None):
        item = ReviewItem(
            kind=ReviewKind.transfer_pair,
            question="Earlier answer",
            payload={"outflow_id": copy.id, "candidates": [inn.id]},
            status=ReviewStatus.resolved,
            resolution={"inflow_id": answer, "resolved_by": "manual"},
        )
        session.add(item)
        await session.flush()
        history.append(item)
    # Existing adjudications can name just the effective latest physical answer.
    # Choose no transfer: resurrecting the older acceptance would split the match.
    adjudication = ReviewItem(
        kind=ReviewKind.transfer_pair,
        question="Adjudicated",
        payload={
            "outflow_id": out.id,
            "candidates": [inn.id],
            "transfer_review_ids": [history[-1].id],
        },
        status=ReviewStatus.resolved,
        resolution={"inflow_id": None, "resolved_by": "manual"},
    )
    session.add(adjudication)
    history.append(adjudication)
    await session.commit()
    original = [(i.payload.copy(), dict(i.resolution or {})) for i in history]
    raw = [
        (t.id, t.account_id, t.posted_on, t.amount_cents, t.description, t.raw.copy())
        for t in [out, copy, inn]
    ]
    for _ in range(2):
        await reconcile_provider_overlap(session, TODAY)
        await rebuild_logical_derived(session, None, TODAY)
        await run_sync(
            session, [RecordingAdapter("plaid"), RecordingAdapter("simplefin")], None, TODAY
        )
        logical = await load_logical_data(session)
        assert [t.id for t in logical.transactions] == [out.id, inn.id]
        assert logical.transaction_roots[copy.id] == out.id
        assert await accrual_spend(session, date(2026, 7, 1), TODAY) == 1000
        assert await accrual_inflows(session, date(2026, 7, 1), TODAY) == 1000
        assert list(await session.scalars(select(TransferLink))) == []
        selected, conflicts = effective_transfer_choices(
            list(reversed(history)), logical.transaction_roots
        )
        assert not conflicts and selected[out.id].inflow_id is None
        assert [(i.payload, i.resolution) for i in history] == original
        assert [
            (t.id, t.account_id, t.posted_on, t.amount_cents, t.description, t.raw)
            for t in [out, copy, inn]
        ] == raw


@pytest.mark.parametrize("root_source", ["plaid", "simplefin"])
@pytest.mark.parametrize("manual", [False, True])
async def test_alias_financing_and_promotion_remain_effective(
    session: AsyncSession, root_source: str, manual: bool
) -> None:
    root = await make_account(
        session,
        source=root_source,
        name="Credit",
        mask="1234",
        type=AccountType.credit,
        balance_cents=-10000,
    )
    alias = await make_account(
        session,
        source="plaid" if root_source == "simplefin" else "simplefin",
        name="Credit",
        mask="1234",
        type=AccountType.credit,
        balance_cents=-10000,
        financing_mode=True,
        promo_expires_on=date(2027, 1, 1),
    )
    item = ReviewItem(
        kind=ReviewKind.financing_account,
        question="Confirmed",
        payload={"account_id": alias.id},
        status=ReviewStatus.resolved,
        resolution={"financing": True, "resolved_by": "manual"},
    )
    session.add(item)
    await session.commit()
    if manual:
        await set_account_link(session, alias.id, root.id)
        await session.commit()
    for _ in range(2):
        await reconcile_provider_overlap(session, TODAY)
        await rebuild_logical_derived(session, None, TODAY)
        logical = await load_logical_data(session)
        assert len(logical.accounts) == 1
        assert logical.accounts[root.id].financing_mode is True
        assert logical.accounts[root.id].promo_expires_on == date(2027, 1, 1)
        assert [o.account_id for o in await compute_obligations(session, TODAY)] == [root.id]
        assert root.financing_mode is False and root.promo_expires_on is None
        assert alias.financing_mode is True and alias.promo_expires_on == date(2027, 1, 1)
        assert item.resolution == {"financing": True, "resolved_by": "manual"}


@pytest.mark.parametrize("root_source", ["plaid", "simplefin"])
@pytest.mark.parametrize("recurring", [False, True])
@pytest.mark.parametrize("automatic_review", [False, True])
@pytest.mark.parametrize("dissolve", ["unlink", "correction"])
async def test_complementary_reviewed_classifications_survive_sync(
    session: AsyncSession, root_source: str, recurring: bool, automatic_review: bool, dissolve: str
) -> None:
    root = await make_account(session, source=root_source)
    alias = await make_account(
        session,
        source="plaid" if root_source == "simplefin" else "simplefin",
        canonical_account_id=root.id,
    )
    originals = []
    history = []
    for month in (5, 6, 7):
        out = await make_txn(
            session,
            root,
            posted_on=date(2026, month, 1),
            description="Coffee House",
            merchant="Coffee House",
        )
        copy = await make_txn(
            session,
            alias,
            posted_on=date(2026, month, 1),
            description="SQ *Coffee House",
            merchant="My Human Merchant",
        )
        originals.extend([out, copy])
        item = ReviewItem(
            kind=ReviewKind.transfer_pair,
            question="Purchase",
            payload={"outflow_id": out.id, "candidates": []},
            status=ReviewStatus.resolved,
            resolution={"inflow_id": None, "resolved_by": "manual"},
        )
        session.add(item)
        history.append(item)
    session.add(
        MerchantAlias(
            raw_descriptor="SQ *Coffee House",
            merchant="My Human Merchant",
            source=AliasSource.manual,
        )
    )
    if automatic_review:
        session.add(
            ReviewItem(
                kind=ReviewKind.merchant,
                question="Old uncertainty",
                payload={"descriptor": "Coffee House", "fallback": "Coffee House"},
            )
        )
    if recurring:
        item = ReviewItem(
            kind=ReviewKind.recurring_cluster,
            question="Habit",
            payload={"merchant": "My Human Merchant", "direction": "outflow"},
            status=ReviewStatus.resolved,
            resolution={"is_recurring": True, "discretionary": True, "resolved_by": "manual"},
        )
        session.add(item)
        history.append(item)
        if automatic_review:
            session.add(
                ReviewItem(
                    kind=ReviewKind.recurring_cluster,
                    question="Weaker recurring question",
                    payload={"merchant": "My Human Merchant", "direction": "outflow"},
                )
            )
    await session.commit()
    raw = [
        (t.id, t.account_id, t.posted_on, t.amount_cents, t.description, t.raw.copy())
        for t in originals
    ]
    old_history = [(i.payload.copy(), dict(i.resolution or {})) for i in history]
    for _ in range(2):
        await run_sync(
            session,
            [RecordingAdapter("plaid"), RecordingAdapter("simplefin")],
            ScriptedLLM(
                {
                    "auto-resolving": {
                        "merchant": "Machine Name",
                        "is_recurring": False,
                        "confident": True,
                    }
                }
            )
            if automatic_review
            else None,
            TODAY,
        )
        logical = await load_logical_data(session)
        assert [(t.id, t.merchant) for t in logical.transactions] == [
            (t.id, "My Human Merchant") for t in originals[::2]
        ]
        if recurring:
            from moneta.models import RecurringSeries

            series = (await session.scalars(select(RecurringSeries))).one()
            assert series.merchant == "My Human Merchant" and series.discretionary
            assert all(t.series_id == series.id for t in logical.transactions)
        assert list(await session.scalars(select(TransferLink))) == []
        assert [(i.payload, i.resolution) for i in history] == old_history
        assert [
            (t.id, t.account_id, t.posted_on, t.amount_cents, t.description, t.raw)
            for t in await session.scalars(select(Transaction).order_by(Transaction.id))
        ] == raw
    if dissolve == "unlink":
        await set_account_link(session, alias.id, None)
    else:
        for txn in originals[1::2]:
            txn.amount_cents = -1100
    await session.commit()
    for _ in range(2):
        await reconcile_provider_overlap(session, TODAY)
        await rebuild_logical_derived(session, None, TODAY)
        logical = await load_logical_data(session)
        assert len(logical.transactions) == 6
        assert [t.merchant for t in originals[::2]] == ["Coffee House"] * 3
        assert [t.merchant for t in originals[1::2]] == ["My Human Merchant"] * 3
        from moneta.models import RecurringSeries

        by_id = {s.id: s for s in await session.scalars(select(RecurringSeries))}
        assert all(
            t.series_id is not None and by_id[t.series_id].merchant == t.merchant for t in originals
        )


async def test_automatic_answer_cannot_replace_legacy_manual_transfer_link(
    session: AsyncSession,
) -> None:
    root = await make_account(session, source="plaid")
    alias = await make_account(session, source="simplefin", canonical_account_id=root.id)
    target = await make_account(session)
    out = await make_txn(session, root)
    copy = await make_txn(session, alias, duplicate_of_id=out.id)
    inn = await make_txn(session, target, amount_cents=1000)
    link = TransferLink(outflow_id=copy.id, inflow_id=inn.id, method="manual", confidence=1.0)
    session.add(link)
    item = ReviewItem(
        kind=ReviewKind.transfer_pair,
        question="Stale",
        payload={"outflow_id": out.id, "candidates": [inn.id]},
    )
    session.add(item)
    await session.commit()
    with pytest.raises(ValueError, match="human"):
        await apply_resolution(session, item, {"inflow_id": None}, resolved_by="llm")
    assert (await session.scalars(select(TransferLink))).one() is link


async def test_incompatible_recurring_merchant_binding_requires_adjudication(
    session: AsyncSession,
) -> None:
    root = await make_account(session, source="plaid")
    alias = await make_account(session, source="simplefin", canonical_account_id=root.id)
    out = await make_txn(session, root, description="Coffee House", merchant="Coffee House")
    copy = await make_txn(session, alias, description="SQ *Coffee House", merchant="Human Cafe")
    session.add(
        MerchantAlias(
            raw_descriptor=copy.description, merchant="Human Cafe", source=AliasSource.manual
        )
    )
    item = ReviewItem(
        kind=ReviewKind.recurring_cluster,
        question="No recurrence",
        payload={"merchant": "Coffee House", "direction": "outflow"},
        status=ReviewStatus.resolved,
        resolution={"is_recurring": False, "resolved_by": "manual"},
    )
    session.add(item)
    await session.commit()
    for _ in range(2):
        await reconcile_provider_overlap(session, TODAY)
        await rebuild_logical_derived(session, None, TODAY)
        assert [(t.id, t.merchant) for t in (await load_logical_data(session)).transactions] == [
            (out.id, "Coffee House"),
            (copy.id, "Human Cafe"),
        ]
        pending = [
            i
            for i in await session.scalars(select(ReviewItem))
            if i.status == ReviewStatus.open and i.kind == ReviewKind.transaction_overlap
        ]
        assert len(pending) == 2
        assert item.resolution == {"is_recurring": False, "resolved_by": "manual"}


async def test_alias_negative_recurring_choice_remains_effective_without_manual_merchant(
    session: AsyncSession,
) -> None:
    from moneta.models import RecurringSeries

    root = await make_account(session, source="simplefin")
    alias = await make_account(session, source="plaid", canonical_account_id=root.id)
    for month in (5, 6, 7):
        out = await make_txn(
            session,
            root,
            posted_on=date(2026, month, 1),
            description="Coffee House",
            merchant="Coffee House",
        )
        await make_txn(
            session,
            alias,
            posted_on=date(2026, month, 1),
            description="SQ *Coffee House",
            merchant="Reviewed Cafe",
        )
        session.add(
            ReviewItem(
                kind=ReviewKind.transfer_pair,
                question="Purchase",
                payload={"outflow_id": out.id},
                status=ReviewStatus.resolved,
                resolution={"inflow_id": None, "resolved_by": "manual"},
            )
        )
    item = ReviewItem(
        kind=ReviewKind.recurring_cluster,
        question="Not recurring",
        payload={"merchant": "Reviewed Cafe", "direction": "outflow"},
        status=ReviewStatus.resolved,
        resolution={"is_recurring": False, "resolved_by": "manual"},
    )
    session.add(item)
    await session.commit()
    for _ in range(2):
        await run_sync(
            session, [RecordingAdapter("plaid"), RecordingAdapter("simplefin")], None, TODAY
        )
        logical = await load_logical_data(session)
        assert [t.merchant for t in logical.transactions] == ["Reviewed Cafe"] * 3
        assert all(t.series_id is None for t in logical.transactions)
        assert list(await session.scalars(select(RecurringSeries))) == []
        assert item.resolution == {"is_recurring": False, "resolved_by": "manual"}


async def test_stronger_root_metadata_remains_authoritative(session: AsyncSession) -> None:
    root = await make_account(
        session, source="plaid", financing_mode=True, promo_expires_on=date(2027, 2, 1)
    )
    alias = await make_account(
        session, source="simplefin", canonical_account_id=root.id, promo_expires_on=date(2027, 1, 1)
    )
    for _ in range(2):
        data = await load_logical_data(session)
        assert data.accounts[root.id].financing_mode is True
        assert data.accounts[root.id].promo_expires_on == date(2027, 2, 1)
        assert alias.promo_expires_on == date(2027, 1, 1)


async def test_unnormalized_survivor_restores_native_merchant_after_unlink(
    session: AsyncSession,
) -> None:
    root = await make_account(session, source="simplefin")
    alias = await make_account(session, source="plaid", canonical_account_id=root.id)
    out = await make_txn(session, root, description="Coffee House")
    copy = await make_txn(session, alias, description="SQ *Coffee House", merchant="Human Cafe")
    session.add(
        MerchantAlias(
            raw_descriptor=copy.description, merchant="Human Cafe", source=AliasSource.manual
        )
    )
    session.add(
        ReviewItem(
            kind=ReviewKind.transfer_pair,
            question="Purchase",
            payload={"outflow_id": out.id},
            status=ReviewStatus.resolved,
            resolution={"inflow_id": None, "resolved_by": "manual"},
        )
    )
    await session.commit()
    await reconcile_provider_overlap(session, TODAY)
    assert out.merchant == "Human Cafe"
    await set_account_link(session, alias.id, None)
    await session.commit()
    for _ in range(2):
        await reconcile_provider_overlap(session, TODAY)
        await rebuild_logical_derived(session, None, TODAY)
        assert out.merchant == "Coffee House"
        assert copy.merchant == "Human Cafe"


@pytest.mark.parametrize("root_annotated", [False, True])
async def test_conflicting_alias_vesting_is_not_fully_vested(
    session: AsyncSession, root_annotated: bool
) -> None:
    root = await make_account(session, source="root", type=AccountType.brokerage)
    first = await make_account(session, source="a", canonical_account_id=root.id)
    second = await make_account(session, source="b", canonical_account_id=root.id)
    holdings = [
        Holding(
            account_id=a.id,
            symbol="TEST",
            quantity=10,
            market_value_cents=100000,
            vested_quantity=v,
            unvested_quantity=10 - v,
        )
        for a, v in [(first, 3), (second, 4)]
    ]
    if root_annotated:
        holdings.append(
            Holding(
                account_id=root.id,
                symbol="TEST",
                quantity=10,
                market_value_cents=100000,
                vested_quantity=2,
                unvested_quantity=8,
            )
        )
    session.add_all(holdings)
    await session.commit()
    for _ in range(2):
        report = await net_worth_report(session)
        assert report.vested_holdings_cents == (20000 if root_annotated else 0)
        assert report.net_worth_cents == (20000 if root_annotated else 0)
        assert report.unvested_potential_cents == (80000 if root_annotated else 0)
        assert report.warnings
        assert [(h.vested_quantity, h.unvested_quantity) for h in holdings[:2]] == [(3, 7), (4, 6)]
        if not root_annotated:
            assert any("excluded" in warning.lower() for warning in report.warnings)


async def test_disappeared_identity_candidates_retire_pending_questions(
    session: AsyncSession,
) -> None:
    from moneta.pipelines.account_overlap import propose_account_links

    first = await make_account(session, source="plaid", name="Checking")
    second = await make_account(session, source="simplefin", name="Checking")
    await propose_account_links(session)
    items = list(await session.scalars(select(ReviewItem)))
    assert len(items) == 2 and all(i.status == ReviewStatus.open for i in items)
    first.mask, second.mask = "1234", "5678"
    for _ in range(2):
        await propose_account_links(session)
        assert all(i.status == ReviewStatus.resolved for i in items)
        assert all((i.resolution or {}).get("superseded_reason") for i in items)
        assert len(list(await session.scalars(select(ReviewItem)))) == 2


async def test_protected_candidates_do_not_label_retained_candidates_as_conflicting(
    session: AsyncSession,
) -> None:
    from moneta.pipelines.account_overlap import propose_account_links

    root = await make_account(session, source="plaid", name="Credit", type=AccountType.credit)
    protected = await make_account(
        session, source="simplefin", name="Credit", type=AccountType.credit, financing_mode=True
    )
    compatible = await make_account(session, source="other", name="Credit", type=AccountType.credit)
    session.add(
        ReviewItem(
            kind=ReviewKind.financing_account,
            question="No",
            payload={"account_id": root.id},
            status=ReviewStatus.resolved,
            resolution={"financing": False, "resolved_by": "manual"},
        )
    )
    await set_account_link(session, protected.id, None)
    await propose_account_links(session)
    pending = [
        i
        for i in await session.scalars(select(ReviewItem))
        if i.status == ReviewStatus.open and i.payload.get("account_id") == root.id
    ]
    assert len(pending) == 1
    assert pending[0].payload["candidates"] == [compatible.id]
    assert not pending[0].payload.get("metadata_conflicts")
