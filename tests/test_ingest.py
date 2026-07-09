from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.aggregator.base import AccountDTO, HoldingDTO, Snapshot, TransactionDTO
from moneta.models import Account, AccountType, Holding, Transaction
from moneta.pipelines.ingest import infer_account_type, ingest_snapshot


def _snap() -> Snapshot:
    return Snapshot(
        accounts=[
            AccountDTO(
                id="ACT-1",
                name="Premier Checking",
                org_name="Chase",
                currency="USD",
                balance=Decimal("1000.00"),
                balance_date=date(2026, 7, 1),
            ),
        ],
        transactions=[
            TransactionDTO(
                id="TRN-1",
                account_id="ACT-1",
                posted_on=date(2026, 7, 2),
                amount=Decimal("-42.50"),
                description="NETFLIX.COM",
                raw={"id": "TRN-1"},
            ),
        ],
        holdings=[
            HoldingDTO(
                account_id="ACT-1", symbol="AAPL", quantity=10.5, market_value=Decimal("2000.00")
            ),
        ],
    )


async def test_ingest_creates_rows(session: AsyncSession) -> None:
    stats = await ingest_snapshot(session, _snap())
    assert stats.new_accounts == 1 and stats.new_transactions == 1 and stats.holdings == 1
    acct = (await session.execute(select(Account))).scalar_one()
    assert acct.balance_cents == 100000
    assert acct.type == AccountType.checking  # inferred from name
    txn = (await session.execute(select(Transaction))).scalar_one()
    assert txn.amount_cents == -4250 and txn.account_id == acct.id


async def test_ingest_is_idempotent(session: AsyncSession) -> None:
    await ingest_snapshot(session, _snap())
    stats = await ingest_snapshot(session, _snap())
    assert stats.new_accounts == 0 and stats.new_transactions == 0
    for model in (Account, Transaction, Holding):
        n = (await session.execute(select(func.count()).select_from(model))).scalar_one()
        assert n == 1


async def test_ingest_updates_balance_not_type(session: AsyncSession) -> None:
    await ingest_snapshot(session, _snap())
    acct = (await session.execute(select(Account))).scalar_one()
    acct.type = AccountType.savings  # user override must survive re-sync
    snap = _snap()
    snap.accounts[0].balance = Decimal("999.00")
    await ingest_snapshot(session, snap)
    acct = (await session.execute(select(Account))).scalar_one()
    assert acct.balance_cents == 99900 and acct.type == AccountType.savings


async def test_ingest_skips_in_snapshot_duplicate_txns(session: AsyncSession) -> None:
    snap = _snap()
    snap.transactions.append(snap.transactions[0].model_copy())
    stats = await ingest_snapshot(session, snap)
    assert stats.new_transactions == 1
    n = (await session.execute(select(func.count()).select_from(Transaction))).scalar_one()
    assert n == 1


def test_infer_account_type() -> None:
    assert infer_account_type("Premier Checking", "Chase") == AccountType.checking
    assert infer_account_type("Online Savings", "Marcus") == AccountType.savings
    assert infer_account_type("Freedom Card", "Chase") == AccountType.credit
    assert infer_account_type("CarCareONE", "Synchrony Bank") == AccountType.loan
    assert infer_account_type("Individual", "Fidelity") == AccountType.brokerage
    assert infer_account_type("Mystery", "Bank") == AccountType.unknown


async def test_type_hint_beats_keyword_inference(session: AsyncSession) -> None:
    snap = Snapshot(
        accounts=[
            AccountDTO(
                id="plaid-1",
                name="Totally Ambiguous Name",
                org_name="Nowhere Bank",
                currency="USD",
                balance=Decimal("10.00"),
                balance_date=date(2026, 7, 1),
                type_hint=AccountType.credit,
            )
        ],
        transactions=[],
        holdings=[],
    )
    await ingest_snapshot(session, snap)
    acct = (await session.execute(select(Account))).scalar_one()
    assert acct.type == AccountType.credit
