"""Accounting consumers must share survivors while retaining physical audit history."""

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.logical import load_logical_data
from moneta.models import (
    AccountType,
    Holding,
    ReviewItem,
    ReviewKind,
    ReviewStatus,
    TransferLink,
)
from moneta.pipelines.reconcile import reconcile_provider_overlap
from moneta.pipelines.recurring import detect_recurring
from moneta.pipelines.transfers import link_transfers
from moneta.queries import primary_currency
from moneta.vesting import apply_vesting, parse_vesting_csv
from moneta.views.cashflow import accrual_by_month, accrual_inflows, accrual_spend, cash_out
from moneta.views.financing import compute_obligations
from moneta.views.networth import net_worth_report
from moneta.views.power import power_report
from moneta.views.transactions import spend_totals, transactions_report
from tests.factories import make_account, make_txn

TODAY = date(2026, 7, 15)


async def test_all_accounting_views_use_the_same_survivors(session: AsyncSession) -> None:
    bank = await make_account(session, source="simplefin", balance_cents=100000)
    bank_alias = await make_account(
        session,
        source="plaid",
        canonical_account_id=bank.id,
        balance_cents=120000,
        balance_date=TODAY,
    )
    loan = await make_account(
        session, source="simplefin", type=AccountType.loan, balance_cents=-30000
    )
    loan_alias = await make_account(
        session,
        source="plaid",
        canonical_account_id=loan.id,
        type=AccountType.unknown,
        balance_cents=-25000,
        balance_date=TODAY,
    )
    broker = await make_account(session, source="simplefin", type=AccountType.brokerage)
    broker_alias = await make_account(
        session,
        source="plaid",
        canonical_account_id=broker.id,
        type=AccountType.brokerage,
        balance_date=TODAY,
    )
    for acct, value in [(broker, 80000), (broker_alias, 90000)]:
        session.add(
            Holding(account_id=acct.id, symbol="TEST", quantity=10, market_value_cents=value)
        )
    for month in [5, 6, 7]:
        for acct, alias, amount, merchant in [
            (bank, bank_alias, 200000, "Payroll"),
            (bank, bank_alias, -5000, "Internet"),
            (bank, bank_alias, -10000, "Loan payment"),
            (loan, loan_alias, 10000, "Loan payment"),
        ]:
            for provider_account in [acct, alias]:
                await make_txn(
                    session,
                    provider_account,
                    posted_on=date(2026, month, 1),
                    amount_cents=amount,
                    description=merchant,
                    merchant=merchant,
                )
    for acct in [bank, bank_alias]:
        await make_txn(
            session,
            acct,
            posted_on=date(2026, 7, 10),
            amount_cents=-2300,
            description="Groceries",
            merchant="Groceries",
        )
    old = await make_txn(
        session, bank_alias, posted_on=date(2025, 7, 1), amount_cents=-1700, merchant="Archive"
    )
    await reconcile_provider_overlap(session, TODAY)
    await link_transfers(session, None)
    await detect_recurring(session, None, TODAY)
    nw = await net_worth_report(session)
    assert (
        nw.liquid_cents,
        nw.liabilities_cents,
        nw.vested_holdings_cents,
        nw.net_worth_cents,
    ) == (120000, 25000, 90000, 185000)
    power = await power_report(session, TODAY)
    assert (power.monthly_income_cents, power.total_fixed_cents, power.spent_so_far_cents) == (
        200000,
        15000,
        2300,
    )
    start, end = date(2026, 7, 1), date(2026, 7, 31)
    assert await accrual_inflows(session, start, end) == 200000
    assert await accrual_spend(session, start, end) == 7300
    assert await cash_out(session, start, end) == 17300
    obligations = await compute_obligations(session, TODAY)
    assert [(o.account_id, o.balance_owed_cents, o.monthly_payment_cents) for o in obligations] == [
        (loan.id, 25000, 10000)
    ]
    assert (await accrual_by_month(session, 13, TODAY))[-1] == ("2025-07", 0, 0, 1700)
    rows = await transactions_report(session, start, end)
    assert len(rows) == 5
    assert spend_totals(rows, start, end, TODAY) == (2300, 2300)
    history = await transactions_report(session, date(2025, 7, 1), end, account_id=bank.id)
    assert old.id in {r.id for r in history}
    alias_history = await transactions_report(
        session, date(2025, 7, 1), end, account_id=bank_alias.id
    )
    assert [r.id for r in alias_history] == [r.id for r in history]
    audit = await transactions_report(session, start, end, include_sources=True)
    assert len(audit) == 10
    assert sum(r.excluded_because == "provider_duplicate" for r in audit) == 5
    assert spend_totals(audit, start, end, TODAY) == (2300, 2300)


async def test_primary_currency_counts_logical_accounts(session: AsyncSession) -> None:
    usd = await make_account(session, source="simplefin", currency="USD")
    await make_account(session, source="plaid", canonical_account_id=usd.id, currency="USD")
    for _ in range(2):
        await make_account(session, currency="EUR")
    assert await primary_currency(session) == "EUR"


async def test_metadata_correction_cannot_relabel_foreign_balance(session: AsyncSession) -> None:
    root = await make_account(session, source="simplefin", currency="USD")
    alias = await make_account(
        session,
        source="plaid",
        canonical_account_id=root.id,
        currency="USD",
        balance_date=TODAY,
        balance_cents=100000,
    )
    alias.currency = "EUR"
    await session.flush()
    with pytest.raises(ValueError, match="currency"):
        await load_logical_data(session)


async def test_same_logical_account_cannot_form_transfer(session: AsyncSession) -> None:
    root = await make_account(session, source="simplefin")
    alias = await make_account(session, source="plaid", canonical_account_id=root.id)
    await make_txn(session, root, amount_cents=-10000, description="Transfer")
    await make_txn(session, alias, amount_cents=10000, description="Transfer")
    assert (await link_transfers(session, None)).linked == 0
    assert list(await session.scalars(select(TransferLink))) == []


@pytest.mark.parametrize("rejected_provider", ["simplefin", "plaid"])
async def test_transfer_rejection_survives_reconciliation(
    session: AsyncSession, rejected_provider: str
) -> None:
    root = await make_account(session, source="simplefin")
    alias = await make_account(session, source="plaid", canonical_account_id=root.id)
    target = await make_account(session, source="simplefin")
    out = await make_txn(session, root, amount_cents=-10000, description="Payment Store")
    duplicate = await make_txn(session, alias, amount_cents=-10000, description="Payment Store")
    inn = await make_txn(session, target, amount_cents=10000, description="Payment Store")
    rejected = out if rejected_provider == "simplefin" else duplicate
    session.add(
        ReviewItem(
            kind=ReviewKind.transfer_pair,
            question="No transfer",
            payload={"outflow_id": rejected.id, "candidates": [inn.id]},
            status=ReviewStatus.resolved,
            resolution={"inflow_id": None, "resolved_by": "manual"},
        )
    )
    await session.commit()
    for _ in range(2):
        await reconcile_provider_overlap(session, TODAY)
        await link_transfers(session, None)
        assert list(await session.scalars(select(TransferLink))) == []


async def test_month_end_duplicate_uses_survivor_posting_date(session: AsyncSession) -> None:
    root = await make_account(session, source="simplefin")
    alias = await make_account(session, source="plaid", canonical_account_id=root.id)
    await make_txn(
        session, root, posted_on=date(2026, 6, 30), amount_cents=-2000, description="Coffee Shop"
    )
    await make_txn(
        session, alias, posted_on=date(2026, 7, 2), amount_cents=-2000, description="Coffee Shop"
    )
    await reconcile_provider_overlap(session, TODAY)
    assert await accrual_by_month(session, 2, TODAY) == [
        ("2026-07", 0, 0, 0),
        ("2026-06", 0, 0, 2000),
    ]


async def test_vesting_updates_count_logical_positions(session: AsyncSession) -> None:
    root = await make_account(session, source="simplefin", type=AccountType.brokerage)
    alias = await make_account(session, source="plaid", canonical_account_id=root.id)
    for acct in [root, alias]:
        session.add(
            Holding(account_id=acct.id, symbol="TEST", quantity=10, market_value_cents=10000)
        )
    await session.flush()
    assert (
        await apply_vesting(
            session, parse_vesting_csv("symbol,vested_quantity,unvested_quantity\nTEST,4,6\n")
        )
        == 1
    )
    report = await net_worth_report(session)
    assert (report.vested_holdings_cents, report.unvested_potential_cents) == (4000, 6000)
    assert report.warnings == []


@pytest.mark.parametrize("rejected_provider", ["simplefin", "plaid"])
async def test_repeated_sync_preserves_source_windows_and_manual_decisions(
    session: AsyncSession, rejected_provider: str
) -> None:
    from moneta.aggregator.base import AggregatorAdapter, Snapshot
    from moneta.models import SyncRun
    from moneta.pipelines.run import run_sync
    from tests.conftest import FakeAdapter

    class FailedAdapter(FakeAdapter):
        async def fetch(self, since: date | None = None) -> Snapshot:
            self.since = since
            raise RuntimeError("offline")

    root = await make_account(session, source="simplefin", balance_cents=50000)
    alias = await make_account(
        session,
        source="plaid",
        canonical_account_id=root.id,
        balance_cents=70000,
        balance_date=TODAY,
    )
    target = await make_account(session, source="simplefin")
    out = await make_txn(
        session, root, posted_on=date(2026, 7, 1), amount_cents=-2300, description="Shop Payment"
    )
    duplicate = await make_txn(
        session, alias, posted_on=date(2026, 7, 3), amount_cents=-2300, description="Shop Payment"
    )
    inn = await make_txn(
        session, target, posted_on=date(2026, 7, 1), amount_cents=2300, description="Shop Payment"
    )
    reviewed = out if rejected_provider == "simplefin" else duplicate
    item = ReviewItem(
        kind=ReviewKind.transfer_pair,
        question="Not a transfer",
        payload={"outflow_id": reviewed.id, "candidates": [inn.id]},
        status=ReviewStatus.resolved,
        resolution={"inflow_id": None, "resolved_by": "manual"},
    )
    session.add(item)
    await session.flush()
    from moneta.pipelines.transaction_overlap import set_transaction_duplicate

    await set_transaction_duplicate(session, duplicate.id, out.id)
    await session.commit()
    empty = Snapshot(accounts=[], transactions=[], holdings=[])
    sf = FakeAdapter(empty, source="simplefin")
    plaid = FakeAdapter(empty, source="plaid")
    failed = FailedAdapter(empty, source="plaid")
    runs: list[list[AggregatorAdapter]] = [[sf, plaid], [plaid, sf], [sf, failed]]
    for adapters in runs:
        report = await run_sync(session, adapters, None, TODAY)
        assert report.overlap.accounts_linked == 0
        assert sf.since == date(2026, 6, 24)
        assert (failed if adapters[-1] is failed else plaid).since == date(2026, 6, 26)
        assert list(await session.scalars(select(TransferLink))) == []
        assert (await net_worth_report(session)).liquid_cents == 70000
        rows = await transactions_report(session, date(2026, 7, 1), TODAY)
        assert spend_totals(rows, date(2026, 7, 1), TODAY, TODAY) == (2300, 2300)
        assert item.resolution == {"inflow_id": None, "resolved_by": "manual"}
    assert "offline" in report.warnings[0]
    from moneta.pipelines.reconcile import rebuild_logical_derived

    audits = [(r.id, r.finished_at) for r in await session.scalars(select(SyncRun))]
    for _ in range(2):
        await reconcile_provider_overlap(session, TODAY)
        await rebuild_logical_derived(session, None, TODAY)
        assert list(await session.scalars(select(TransferLink))) == []
    assert [(r.id, r.finished_at) for r in await session.scalars(select(SyncRun))] == audits


async def test_events_and_review_samples_ignore_duplicate_occurrences(
    session: AsyncSession,
) -> None:
    from moneta.models import SeriesEvent
    from moneta.pipelines.events import emit_series_events
    from moneta.pipelines.review import review_context
    from tests.factories import make_series

    root = await make_account(session, source="simplefin")
    alias = await make_account(session, source="plaid", canonical_account_id=root.id)
    series = await make_series(
        session, merchant="Bill", expected_cents=-1000, next_expected_on=date(2026, 8, 1)
    )
    await make_txn(
        session,
        root,
        merchant="Bill",
        series_id=series.id,
        posted_on=date(2026, 6, 1),
        amount_cents=-1000,
    )
    new = await make_txn(
        session,
        root,
        merchant="Bill",
        series_id=series.id,
        posted_on=date(2026, 7, 1),
        amount_cents=-2000,
    )
    await make_txn(
        session,
        alias,
        merchant="Bill",
        series_id=series.id,
        posted_on=date(2026, 7, 1),
        amount_cents=-2000,
        duplicate_of_id=new.id,
    )
    item = ReviewItem(
        kind=ReviewKind.recurring_cluster,
        question="Bill?",
        payload={"merchant": "Bill", "direction": "outflow"},
    )
    assert await emit_series_events(session, None, TODAY) == 0
    context = await review_context(session, item)
    assert context["samples"] == [
        {"posted_on": "2026-07-01", "amount_cents": -2000},
        {"posted_on": "2026-06-01", "amount_cents": -1000},
    ]
    assert await emit_series_events(session, None, TODAY) == 0
    assert series.expected_cents == -1000
    assert list(await session.scalars(select(SeriesEvent))) == []


async def test_financing_detection_combines_alias_only_payment_history(
    session: AsyncSession,
) -> None:
    from moneta.pipelines.financing import detect_financing

    root = await make_account(
        session, source="simplefin", type=AccountType.credit, balance_cents=-10000
    )
    alias = await make_account(
        session, source="plaid", canonical_account_id=root.id, type=AccountType.unknown
    )
    await make_txn(session, alias, posted_on=date(2026, 6, 1), amount_cents=1000)
    await make_txn(session, root, posted_on=date(2026, 7, 1), amount_cents=1000)
    assert await detect_financing(session) == 1
    item = (await session.scalars(select(ReviewItem))).one()
    assert item.payload == {"account_id": root.id}
    assert await detect_financing(session) == 0


async def test_orphaned_duplicate_series_retires_before_rebuild(session: AsyncSession) -> None:
    from moneta.models import SeriesStatus
    from tests.factories import make_series

    root = await make_account(session, source="simplefin")
    alias = await make_account(session, source="plaid", canonical_account_id=root.id)
    orphan = await make_series(session, merchant="Legacy Bill", expected_cents=-5000)
    for month in [5, 6, 7]:
        await make_txn(
            session,
            root,
            posted_on=date(2026, month, 1),
            amount_cents=-5000,
            description="Internet",
            merchant="Internet",
        )
        await make_txn(
            session,
            alias,
            posted_on=date(2026, month, 1),
            amount_cents=-5000,
            description="Internet",
            merchant="Legacy Bill",
            series_id=orphan.id,
        )
    await reconcile_provider_overlap(session, TODAY)
    from moneta.pipelines.reconcile import rebuild_logical_derived

    await rebuild_logical_derived(session, None, TODAY)
    assert orphan.status == SeriesStatus.ended
    assert (await power_report(session, TODAY)).total_fixed_cents == 5000


async def test_review_resolutions_target_logical_endpoints(session: AsyncSession) -> None:
    from moneta.pipelines.review import apply_resolution

    root = await make_account(session, source="simplefin", type=AccountType.credit)
    alias = await make_account(session, source="plaid", canonical_account_id=root.id)
    financing = ReviewItem(
        kind=ReviewKind.financing_account, question="Financing?", payload={"account_id": alias.id}
    )
    await apply_resolution(session, financing, {"financing": True})
    bank = await make_account(session, source="simplefin")
    out = await make_txn(session, bank, amount_cents=-1000)
    inn = await make_txn(session, root, amount_cents=1000)
    duplicate = await make_txn(session, alias, amount_cents=1000, duplicate_of_id=inn.id)
    item = ReviewItem(
        kind=ReviewKind.transfer_pair,
        question="Transfer?",
        payload={"outflow_id": out.id, "candidates": [duplicate.id]},
    )
    await apply_resolution(session, item, {"inflow_id": duplicate.id})
    await session.flush()
    link = (await session.scalars(select(TransferLink))).one()
    assert (link.outflow_id, link.inflow_id) == (out.id, inn.id)
    assert root.financing_mode is True
    assert item.resolution == {"inflow_id": duplicate.id, "resolved_by": "manual"}
    await apply_resolution(session, item, {"inflow_id": None})
    await session.flush()
    assert list(await session.scalars(select(TransferLink))) == []


@pytest.mark.parametrize("resolved_by,method", [("manual", "manual"), ("llm", "llm")])
async def test_reviewed_transfer_keeps_priority_and_provenance(
    session: AsyncSession, resolved_by: str, method: str
) -> None:
    bank = await make_account(session, source="simplefin")
    other = await make_account(session, source="plaid")
    chosen = await make_txn(
        session, bank, posted_on=date(2026, 7, 1), amount_cents=-1000, description="Unclear"
    )
    competitor = await make_txn(
        session, bank, posted_on=date(2026, 7, 5), amount_cents=-1000, description="Transfer"
    )
    inn = await make_txn(
        session, other, posted_on=date(2026, 7, 5), amount_cents=1000, description="Receipt"
    )
    session.add(
        ReviewItem(
            kind=ReviewKind.transfer_pair,
            question="Chosen",
            payload={"outflow_id": chosen.id, "candidates": [inn.id]},
            status=ReviewStatus.resolved,
            resolution={"inflow_id": inn.id, "resolved_by": resolved_by},
        )
    )
    await session.commit()
    await link_transfers(session, None)
    link = (await session.scalars(select(TransferLink))).one()
    assert (link.outflow_id, link.inflow_id, link.method) == (chosen.id, inn.id, method)
    assert competitor.id != link.outflow_id


@pytest.mark.parametrize("human_source", ["simplefin", "plaid"])
@pytest.mark.parametrize("human_accepts", [False, True])
async def test_alias_transfer_decisions_reduce_human_precedence_after_mapping(
    session: AsyncSession, human_source: str, human_accepts: bool
) -> None:
    from moneta.pipelines.reconcile import rebuild_logical_derived
    from moneta.pipelines.transaction_overlap import set_transaction_duplicate

    root = await make_account(session, source="simplefin")
    alias = await make_account(session, source="plaid", canonical_account_id=root.id)
    target = await make_account(session, source="simplefin")
    out = await make_txn(session, root, amount_cents=-1000, description="Payment Store")
    copy = await make_txn(session, alias, amount_cents=-1000, description="Payment Store")
    inn = await make_txn(session, target, amount_cents=1000, description="Payment Store")
    human, machine = (out, copy) if human_source == "simplefin" else (copy, out)
    human_item = ReviewItem(
        kind=ReviewKind.transfer_pair,
        question="human",
        payload={"outflow_id": human.id, "candidates": [inn.id]},
        status=ReviewStatus.resolved,
        resolution={"inflow_id": inn.id if human_accepts else None, "resolved_by": "manual"},
    )
    llm_item = ReviewItem(
        kind=ReviewKind.transfer_pair,
        question="machine",
        payload={"outflow_id": machine.id, "candidates": [inn.id]},
        status=ReviewStatus.resolved,
        resolution={"inflow_id": None if human_accepts else inn.id, "resolved_by": "llm"},
    )
    session.add_all([human_item, llm_item])
    await session.flush()
    await set_transaction_duplicate(session, copy.id, out.id)
    await session.commit()
    history = [
        (i.payload.copy(), i.resolution.copy() if i.resolution else None)
        for i in [human_item, llm_item]
    ]
    for _ in range(3):
        await reconcile_provider_overlap(session, TODAY)
        await rebuild_logical_derived(session, None, TODAY)
        links = list(await session.scalars(select(TransferLink)))
        assert [(link.outflow_id, link.inflow_id, link.method) for link in links] == (
            [(out.id, inn.id, "manual")] if human_accepts else []
        )
    assert [(i.payload, i.resolution) for i in [human_item, llm_item]] == history


@pytest.mark.parametrize("pending_review", [False, True])
@pytest.mark.parametrize("resolved_by", ["manual", "llm"])
async def test_conflicting_alias_transfer_decisions_require_one_review(
    session: AsyncSession, resolved_by: str, pending_review: bool
) -> None:
    from moneta.pipelines.reconcile import rebuild_logical_derived
    from moneta.pipelines.review import apply_resolution
    from moneta.pipelines.transaction_overlap import set_transaction_duplicate

    root = await make_account(session, source="simplefin")
    alias = await make_account(session, source="plaid", canonical_account_id=root.id)
    target = await make_account(session, source="simplefin")
    out = await make_txn(session, root, amount_cents=-1000, description="Payment Store")
    copy = await make_txn(session, alias, amount_cents=-1000, description="Payment Store")
    inn = await make_txn(session, target, amount_cents=1000, description="Payment Store")
    if pending_review:
        session.add(
            ReviewItem(
                kind=ReviewKind.transfer_pair,
                question="earlier open question",
                payload={"outflow_id": out.id, "candidates": [inn.id]},
            )
        )
        await session.flush()
    old = []
    for txn, choice in [(out, None), (copy, inn.id)]:
        item = ReviewItem(
            kind=ReviewKind.transfer_pair,
            question="old",
            payload={"outflow_id": txn.id, "candidates": [inn.id]},
            status=ReviewStatus.resolved,
            resolution={"inflow_id": choice, "resolved_by": resolved_by},
        )
        session.add(item)
        old.append(item)
    await session.flush()
    await set_transaction_duplicate(session, copy.id, out.id)
    await session.commit()
    history = [(i.payload.copy(), i.resolution.copy() if i.resolution else None) for i in old]
    for _ in range(2):
        await reconcile_provider_overlap(session, TODAY)
        await rebuild_logical_derived(session, None, TODAY)
        assert list(await session.scalars(select(TransferLink))) == []
        pending = list(
            await session.scalars(
                select(ReviewItem).where(
                    ReviewItem.kind == ReviewKind.transfer_pair,
                    ReviewItem.status == ReviewStatus.open,
                )
            )
        )
        assert len(pending) == 1
        assert pending[0].payload["outflow_id"] == out.id
        assert pending[0].payload["candidates"] == [inn.id]
        assert pending[0].payload["llm_flagged"] is True
    await apply_resolution(session, pending[0], {"inflow_id": inn.id})
    await session.commit()
    for _ in range(2):
        await reconcile_provider_overlap(session, TODAY)
        await rebuild_logical_derived(session, None, TODAY)
        links = list(await session.scalars(select(TransferLink)))
        assert [(link.outflow_id, link.inflow_id, link.method) for link in links] == [
            (out.id, inn.id, "manual")
        ]
    assert [(i.payload, i.resolution) for i in old] == history


@pytest.mark.parametrize("shared_merchant", [False, True])
async def test_currency_flip_filters_series_samples_and_restores_reviewed_history(
    session: AsyncSession, shared_merchant: bool
) -> None:
    from moneta.models import RecurringSeries
    from moneta.pipelines.reconcile import rebuild_logical_derived
    from moneta.pipelines.review import review_context

    usd = await make_account(session, source="simplefin", currency="USD")
    alias = await make_account(session, source="plaid", currency="USD")
    eur = await make_account(session, currency="EUR")
    await make_account(session, currency="EUR")
    eur_merchant = "Bill" if shared_merchant else "Euro Bill"
    for month in [5, 6, 7]:
        await make_txn(
            session,
            usd,
            posted_on=date(2026, month, 1),
            amount_cents=-1000,
            merchant="Bill",
            description="Bill",
        )
        await make_txn(
            session,
            eur,
            posted_on=date(2026, month, 1),
            amount_cents=-2000,
            merchant=eur_merchant,
            description=eur_merchant,
        )
    reviewed = ReviewItem(
        kind=ReviewKind.recurring_cluster,
        question="bill",
        payload={"merchant": "Bill", "direction": "outflow"},
        status=ReviewStatus.resolved,
        resolution={"is_recurring": True, "resolved_by": "manual"},
    )
    session.add(reviewed)
    await session.commit()
    assert await primary_currency(session) == "USD"
    await rebuild_logical_derived(session, None, TODAY)
    assert (await power_report(session, TODAY)).total_fixed_cents == 1000
    alias.canonical_account_id = usd.id
    await session.commit()
    assert await primary_currency(session) == "EUR"
    for _ in range(2):
        await rebuild_logical_derived(session, None, TODAY)
        assert (await power_report(session, TODAY)).total_fixed_cents == 2000
        context = await review_context(session, reviewed)
        assert [s["amount_cents"] for s in context.get("samples", [])] == (
            [-2000] * 3 if shared_merchant else []
        )
    alias.canonical_account_id = None
    await session.commit()
    for _ in range(2):
        await rebuild_logical_derived(session, None, TODAY)
        assert (await power_report(session, TODAY)).total_fixed_cents == 1000
    assert reviewed.resolution == {"is_recurring": True, "resolved_by": "manual"}
    assert len(list(await session.scalars(select(RecurringSeries)))) == (
        1 if shared_merchant else 2
    )


async def test_foreign_series_events_and_verification_do_not_use_old_currency(
    session: AsyncSession,
) -> None:
    from moneta.models import SeriesEvent
    from moneta.pipelines.events import emit_series_events
    from moneta.pipelines.review import review_context, verify_series
    from tests.factories import make_price_change_item, make_series
    from tests.test_autoreview import ScriptedLLM

    usd = await make_account(session, currency="USD")
    await make_account(session, currency="EUR")
    await make_account(session, currency="EUR")
    series = await make_series(
        session, merchant="Foreign Bill", expected_cents=-1000, next_expected_on=date(2026, 6, 1)
    )
    for month in [6, 7]:
        await make_txn(
            session,
            usd,
            posted_on=date(2026, month, 1),
            amount_cents=-2000,
            merchant="Foreign Bill",
            series_id=series.id,
        )
    llm = ScriptedLLM({"Foreign Bill": {"classification": "bill", "confident": True}})
    assert (await verify_series(session, llm)).verified == 0
    assert list(await session.scalars(select(ReviewItem))) == []
    assert await emit_series_events(session, None, TODAY) == 0
    assert series.expected_cents == -1000
    assert series.next_expected_on == date(2026, 6, 1)
    assert list(await session.scalars(select(SeriesEvent))) == []
    item = make_price_change_item(
        series.id,
        payload={
            "series_id": series.id,
            "merchant": "Foreign Bill",
            "old_cents": -1000,
            "new_cents": -2000,
        },
    )
    context = await review_context(session, item)
    assert context == {}


async def test_foreign_loan_payments_do_not_contribute_to_primary_power(
    session: AsyncSession,
) -> None:
    bank = await make_account(session, currency="USD")
    loan = await make_account(session, currency="USD", type=AccountType.loan, balance_cents=-10000)
    for _ in range(3):
        await make_account(session, currency="EUR")
    await make_txn(session, bank, amount_cents=-1000, description="Loan Payment")
    await make_txn(session, loan, amount_cents=1000, description="Loan Payment")
    await link_transfers(session, None)
    assert (await power_report(session, TODAY)).total_fixed_cents == 0
    assert await compute_obligations(session, TODAY) == []


@pytest.mark.parametrize("currency_evidence", [False, True])
async def test_old_currency_price_review_cannot_reprice_rebuilt_series(
    session: AsyncSession, currency_evidence: bool
) -> None:
    from moneta.models import SeriesEvent
    from moneta.pipelines.review import apply_resolution
    from tests.factories import make_series

    usd = await make_account(session, currency="USD")
    eur = await make_account(session, currency="EUR")
    await make_account(session, currency="EUR")
    series = await make_series(session, merchant="Bill", expected_cents=-2000)
    old = await make_txn(
        session, usd, merchant="Bill", amount_cents=-1200, posted_on=date(2026, 6, 1)
    )
    await make_txn(session, eur, merchant="Bill", amount_cents=-2000, series_id=series.id)
    payload = {
        "series_id": series.id,
        "merchant": "Bill",
        "old_cents": -1000,
        "new_cents": -1200,
        "occurred_on": "2026-06-01",
    }
    if currency_evidence:
        payload.update({"currency": "USD", "transaction_id": old.id})
    item = ReviewItem(kind=ReviewKind.price_change, question="Old USD price", payload=payload)
    session.add(item)
    await session.commit()
    with pytest.raises(ValueError, match="currency"):
        await apply_resolution(session, item, {"is_price_change": True})
    assert series.expected_cents == -2000
    assert item.status == ReviewStatus.open
    assert list(await session.scalars(select(SeriesEvent))) == []


async def test_currency_transition_preserves_legacy_price_review_denomination(
    session: AsyncSession,
) -> None:
    from moneta.models import RecurringSeries
    from moneta.pipelines.review import apply_resolution

    usd = await make_account(session, currency="USD", source="simplefin")
    alias = await make_account(session, currency="USD", source="plaid")
    eur = await make_account(session, currency="EUR")
    await make_account(session, currency="EUR")
    for month in [5, 6, 7]:
        await make_txn(
            session, usd, posted_on=date(2026, month, 1), amount_cents=-1000, merchant="Bill"
        )
        await make_txn(
            session, eur, posted_on=date(2026, month, 1), amount_cents=-2000, merchant="Bill"
        )
    await detect_recurring(session, None, TODAY)
    series = (await session.scalars(select(RecurringSeries))).one()
    # The provider corrected the old observation; the legacy request still carries
    # its original proposal, without modern transaction/currency evidence.
    request = ReviewItem(
        kind=ReviewKind.price_change,
        question="old price",
        payload={
            "series_id": series.id,
            "merchant": "Bill",
            "old_cents": -1000,
            "new_cents": -1200,
            "occurred_on": "2026-07-01",
        },
    )
    session.add(request)
    alias.canonical_account_id = usd.id
    await session.commit()
    await detect_recurring(session, None, TODAY)
    assert series.expected_cents == -2000
    with pytest.raises(ValueError, match="currency"):
        await apply_resolution(session, request, {"is_price_change": True})
    assert series.expected_cents == -2000


async def test_currency_switch_never_rewinds_series_event_cursor(session: AsyncSession) -> None:
    from moneta.models import RecurringSeries

    usd = await make_account(session, currency="USD", source="simplefin")
    alias = await make_account(session, currency="USD", source="plaid")
    eur = await make_account(session, currency="EUR")
    await make_account(session, currency="EUR")
    for month in [5, 6, 7]:
        await make_txn(
            session, usd, posted_on=date(2026, month, 1), amount_cents=-1000, merchant="Bill"
        )
        await make_txn(
            session, eur, posted_on=date(2026, month, 1), amount_cents=-2000, merchant="Bill"
        )
    await detect_recurring(session, None, TODAY)
    series = (await session.scalars(select(RecurringSeries))).one()
    series.next_expected_on = date(2026, 9, 1)
    alias.canonical_account_id = usd.id
    await session.commit()
    await detect_recurring(session, None, TODAY)
    assert series.next_expected_on == date(2026, 9, 1)


@pytest.mark.parametrize("alias_source", ["plaid", "simplefin"])
async def test_transfer_adjudication_does_not_resurrect_superseded_answers(
    session: AsyncSession, alias_source: str
) -> None:
    from moneta.pipelines.reconcile import rebuild_logical_derived
    from moneta.pipelines.review import apply_resolution
    from moneta.pipelines.transaction_overlap import set_transaction_duplicate
    from moneta.pipelines.transfers import effective_transfer_choices

    root = await make_account(session, source="simplefin" if alias_source == "plaid" else "plaid")
    alias = await make_account(session, source=alias_source, canonical_account_id=root.id)
    target = await make_account(session, source=root.source)
    out = await make_txn(session, root, amount_cents=-1000, description="Payment Store")
    copy = await make_txn(session, alias, amount_cents=-1000, description="Payment Store")
    inn = await make_txn(session, target, amount_cents=1000, description="Payment Store")
    history: list[ReviewItem] = []
    for txn, choice, resolved_by in [
        (out, inn.id, "manual"),
        (out, None, "manual"),
        (copy, None, "manual"),
        (copy, inn.id, "manual"),
        (out, inn.id, "llm"),
        (copy, None, "llm"),
    ]:
        item = ReviewItem(
            kind=ReviewKind.transfer_pair,
            question="prior answer",
            payload={"outflow_id": txn.id, "candidates": [inn.id]},
            status=ReviewStatus.resolved,
            resolution={"inflow_id": choice, "resolved_by": resolved_by},
        )
        session.add(item)
        history.append(item)
    await session.flush()
    await set_transaction_duplicate(session, copy.id, out.id)
    await session.commit()
    original = [(item.payload.copy(), dict(item.resolution or {})) for item in history]
    observations = [
        (txn.id, txn.account_id, txn.amount_cents, txn.posted_on, txn.description, txn.raw.copy())
        for txn in [out, copy, inn]
    ]
    await reconcile_provider_overlap(session, TODAY)
    await rebuild_logical_derived(session, None, TODAY)
    question = (
        await session.scalars(
            select(ReviewItem).where(
                ReviewItem.kind == ReviewKind.transfer_pair, ReviewItem.status == ReviewStatus.open
            )
        )
    ).one()
    assert question.payload["transfer_review_ids"] == [history[1].id, history[3].id]
    await apply_resolution(session, question, {"inflow_id": inn.id})
    await session.commit()
    # Even answers newer than adjudication cannot upgrade LLM authority to human.
    for txn in [out, copy]:
        session.add(
            ReviewItem(
                kind=ReviewKind.transfer_pair,
                question="later machine answer",
                payload={"outflow_id": txn.id, "candidates": [inn.id]},
                status=ReviewStatus.resolved,
                resolution={"inflow_id": None, "resolved_by": "llm"},
            )
        )
    await session.commit()

    for _ in range(3):
        await reconcile_provider_overlap(session, TODAY)
        await rebuild_logical_derived(session, None, TODAY)
        links = list(await session.scalars(select(TransferLink)))
        assert [
            (link.outflow_id, link.inflow_id, link.method, link.confidence) for link in links
        ] == [(out.id, inn.id, "manual", 1.0)]
        assert (
            list(
                await session.scalars(
                    select(ReviewItem).where(
                        ReviewItem.kind == ReviewKind.transfer_pair,
                        ReviewItem.status == ReviewStatus.open,
                    )
                )
            )
            == []
        )
    logical = await load_logical_data(session)
    decisions, conflicts = effective_transfer_choices(
        list(reversed(list(await session.scalars(select(ReviewItem))))), logical.transaction_roots
    )
    assert conflicts == {}
    assert (decisions[out.id].inflow_id, decisions[out.id].method) == (inn.id, "manual")

    # A genuinely new human answer on the alias is not covered by adjudication.
    newer = ReviewItem(
        kind=ReviewKind.transfer_pair,
        question="new human answer",
        payload={"outflow_id": copy.id, "candidates": [inn.id]},
        status=ReviewStatus.resolved,
        resolution={"inflow_id": None, "resolved_by": "manual"},
    )
    session.add(newer)
    await session.commit()
    await reconcile_provider_overlap(session, TODAY)
    await rebuild_logical_derived(session, None, TODAY)
    assert list(await session.scalars(select(TransferLink))) == []
    new_question = (
        await session.scalars(
            select(ReviewItem).where(
                ReviewItem.kind == ReviewKind.transfer_pair, ReviewItem.status == ReviewStatus.open
            )
        )
    ).one()
    assert new_question.payload["transfer_review_ids"] == [question.id, newer.id]
    await apply_resolution(session, new_question, {"inflow_id": None})
    await session.commit()
    for _ in range(2):
        await reconcile_provider_overlap(session, TODAY)
        await rebuild_logical_derived(session, None, TODAY)
        assert list(await session.scalars(select(TransferLink))) == []
        assert (
            list(
                await session.scalars(
                    select(ReviewItem).where(
                        ReviewItem.kind == ReviewKind.transfer_pair,
                        ReviewItem.status == ReviewStatus.open,
                    )
                )
            )
            == []
        )
    assert [(item.payload, item.resolution) for item in history] == original
    assert [
        (txn.id, txn.account_id, txn.amount_cents, txn.posted_on, txn.description, txn.raw)
        for txn in [out, copy, inn]
    ] == observations
