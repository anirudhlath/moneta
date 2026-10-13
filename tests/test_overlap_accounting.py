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
from moneta.views.cashflow import accrual_by_month, accrual_income, accrual_spend, cash_out
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
    assert await accrual_income(session, start, end) == 200000
    assert await accrual_spend(session, start, end) == 7300
    assert await cash_out(session, start, end) == 17300
    obligations = await compute_obligations(session, TODAY)
    assert [(o.account_id, o.balance_owed_cents, o.monthly_payment_cents) for o in obligations] == [
        (loan.id, 25000, 10000)
    ]
    assert (await accrual_by_month(session, 13, TODAY))[-1] == ("2025-07", 0, 1700)
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
    assert await accrual_by_month(session, 2, TODAY) == [("2026-07", 0, 0), ("2026-06", 0, 2000)]


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
