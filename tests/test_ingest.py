from datetime import date
from decimal import Decimal

import pytest
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


async def test_ingest_stamps_source_on_create_and_backfills_on_update(
    session: AsyncSession,
) -> None:
    snap = _snap()
    snap.accounts[0].source = "simplefin"
    await ingest_snapshot(session, snap)
    acct = (await session.execute(select(Account))).scalar_one()
    assert acct.source == "simplefin"

    # simulate a pre-existing account synced before source tracking existed
    acct.source = ""
    await session.commit()
    await ingest_snapshot(session, snap)  # re-sync naturally backfills it
    acct = (await session.execute(select(Account))).scalar_one()
    assert acct.source == "simplefin"


async def test_ingest_roundtrips_mask_metadata(session: AsyncSession) -> None:
    snap = _snap()
    snap.accounts[0] = snap.accounts[0].model_copy(update={"mask": "0000"})
    await ingest_snapshot(session, snap)
    acct = (await session.scalars(select(Account))).one()
    assert acct.mask == "0000"
    assert acct.canonical_account_id is None
    txn = (await session.scalars(select(Transaction))).one()
    assert txn.duplicate_of_id is None

    snap.accounts[0] = snap.accounts[0].model_copy(update={"mask": "1234"})
    await ingest_snapshot(session, snap)
    await session.refresh(acct)
    assert acct.mask == "1234"
    assert (await session.scalar(select(func.count()).select_from(Account))) == 1


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


async def test_resynced_txn_with_changed_fields_is_updated(session: AsyncSession) -> None:
    acct = AccountDTO(
        id="A-1",
        name="Checking",
        org_name="Chase",
        currency="USD",
        balance=Decimal("100.00"),
        balance_date=date(2026, 7, 1),
    )

    def snap(amount: str, desc: str) -> Snapshot:
        return Snapshot(
            accounts=[acct],
            transactions=[
                TransactionDTO(
                    id="T-1",
                    account_id="A-1",
                    posted_on=date(2026, 7, 1),
                    amount=Decimal(amount),
                    description=desc,
                    raw={},
                )
            ],
            holdings=[],
        )

    await ingest_snapshot(session, snap("-10.00", "COFFEE"))
    stats = await ingest_snapshot(session, snap("-12.34", "COFFEE SHOP"))
    assert stats.new_transactions == 0
    assert stats.updated_transactions == 1
    txn = (await session.execute(select(Transaction))).scalar_one()
    assert txn.amount_cents == -1234
    assert txn.description == "COFFEE SHOP"

    stats = await ingest_snapshot(session, snap("-12.34", "COFFEE SHOP"))
    assert stats.updated_transactions == 0  # identical re-sync is a no-op


async def test_description_correction_clears_merchant_keeps_series(session: AsyncSession) -> None:
    from moneta.pipelines.normalize import normalize_merchants
    from tests.factories import make_series

    acct = AccountDTO(
        id="A-1",
        name="Checking",
        org_name="Chase",
        currency="USD",
        balance=Decimal("100.00"),
        balance_date=date(2026, 7, 1),
    )

    def snap(desc: str) -> Snapshot:
        return Snapshot(
            accounts=[acct],
            transactions=[
                TransactionDTO(
                    id="T-1",
                    account_id="A-1",
                    posted_on=date(2026, 7, 1),
                    amount=Decimal("-15.99"),
                    description=desc,
                    raw={},
                )
            ],
            holdings=[],
        )

    await ingest_snapshot(session, snap("NETFLIX.COM"))
    await normalize_merchants(session, llm=None)
    txn = (await session.execute(select(Transaction))).scalar_one()
    assert txn.merchant == "Netflix.Com"
    series = await make_series(session, merchant="Netflix.Com")
    txn.series_id = series.id
    await session.commit()

    await ingest_snapshot(session, snap("SPOTIFY USA"))
    txn = (await session.execute(select(Transaction))).scalar_one()
    assert txn.merchant is None  # stale merchant cleared; normalize re-derives next
    assert txn.series_id == series.id  # untagging is detection's call, not ingest's
    await normalize_merchants(session, llm=None)
    await session.refresh(txn)
    assert txn.merchant == "Spotify"


async def test_ingest_skips_txn_and_holding_with_unknown_account_id(session: AsyncSession) -> None:
    """A txn/holding whose account_id isn't in this snapshot's accounts (e.g. an
    institution mid-migration handing back a stale id) must be skipped, not crash
    the acct_ids[...] lookup."""
    snap = _snap()
    snap.transactions.append(
        TransactionDTO(
            id="TRN-ORPHAN",
            account_id="ACT-GHOST",
            posted_on=date(2026, 7, 2),
            amount=Decimal("-5.00"),
            description="ORPHAN TXN",
            raw={},
        )
    )
    snap.holdings.append(
        HoldingDTO(
            account_id="ACT-GHOST", symbol="GHOST", quantity=1.0, market_value=Decimal("10.00")
        )
    )
    stats = await ingest_snapshot(session, snap)
    assert stats.new_accounts == 1
    assert stats.new_transactions == 1  # only the real ACT-1 txn
    assert stats.holdings == 1  # only the real ACT-1 holding
    txn_ids = {t.aggregator_id for t in (await session.execute(select(Transaction))).scalars()}
    assert txn_ids == {"TRN-1"}
    holding_symbols = {h.symbol for h in (await session.execute(select(Holding))).scalars()}
    assert holding_symbols == {"AAPL"}


async def test_ingest_fully_empty_snapshot_is_noop(session: AsyncSession) -> None:
    empty = Snapshot(accounts=[], transactions=[], holdings=[])
    stats = await ingest_snapshot(session, empty)
    assert stats.new_accounts == 0
    assert stats.new_transactions == 0
    assert stats.updated_transactions == 0
    assert stats.holdings == 0
    for model in (Account, Transaction, Holding):
        n = (await session.execute(select(func.count()).select_from(model))).scalar_one()
        assert n == 0


async def test_amount_correction_drops_transfer_link(session: AsyncSession) -> None:
    from moneta.models import TransferLink

    accts = [
        AccountDTO(
            id=aid,
            name=name,
            org_name="Chase",
            currency="USD",
            balance=Decimal("100.00"),
            balance_date=date(2026, 7, 1),
        )
        for aid, name in (("A-1", "Checking"), ("A-2", "Savings"))
    ]

    def snap(amount: str) -> Snapshot:
        return Snapshot(
            accounts=accts,
            transactions=[
                TransactionDTO(
                    id="T-OUT",
                    account_id="A-1",
                    posted_on=date(2026, 7, 1),
                    amount=Decimal(amount),
                    description="TRANSFER OUT",
                    raw={},
                ),
                TransactionDTO(
                    id="T-IN",
                    account_id="A-2",
                    posted_on=date(2026, 7, 1),
                    amount=Decimal("500.00"),
                    description="TRANSFER IN",
                    raw={},
                ),
            ],
            holdings=[],
        )

    await ingest_snapshot(session, snap("-500.00"))
    rows = (await session.execute(select(Transaction))).scalars().all()
    out = next(t for t in rows if t.aggregator_id == "T-OUT")
    inn = next(t for t in rows if t.aggregator_id == "T-IN")
    session.add(TransferLink(outflow_id=out.id, inflow_id=inn.id, confidence=1.0, method="rule"))
    await session.commit()

    await ingest_snapshot(session, snap("-480.00"))  # upstream correction: legs no longer equal
    assert (await session.execute(select(TransferLink))).scalars().all() == []


@pytest.mark.parametrize("correction", ["amount", "date", "description"])
async def test_correction_invalidates_affected_duplicate_relationships(
    session: AsyncSession, correction: str
) -> None:
    from moneta.models import TransferLink
    from tests.factories import make_account, make_txn

    snap = _snap()
    snap.accounts[0].source = "simplefin"
    await ingest_snapshot(session, snap)
    corrected = (await session.scalars(select(Transaction))).one()
    plaid = await make_account(session, aggregator_id="plaid-overlap-account", source="plaid")
    root = await make_txn(session, plaid, amount_cents=-4250)
    corrected.duplicate_of_id = root.id
    dependent = await make_txn(session, plaid, duplicate_of_id=corrected.id)
    unrelated = await make_txn(session, plaid, duplicate_of_id=root.id)
    payment = await make_txn(session, plaid, amount_cents=4250)
    session.add(
        TransferLink(outflow_id=corrected.id, inflow_id=payment.id, confidence=1.0, method="rule")
    )
    await session.commit()

    if correction == "amount":
        snap.transactions[0].amount = Decimal("-40.00")
    elif correction == "date":
        snap.transactions[0].posted_on = date(2026, 7, 3)
    else:
        snap.transactions[0].description = "NETFLIX UPDATED"
    snap.transactions[0].raw = {"id": "TRN-1", "corrected": True}
    stats = await ingest_snapshot(session, snap)

    for txn in (corrected, dependent, unrelated, root, payment):
        await session.refresh(txn)
    if correction == "description":
        assert corrected.duplicate_of_id == root.id
        assert dependent.duplicate_of_id == corrected.id
        assert len((await session.scalars(select(TransferLink))).all()) == 1
    else:
        assert corrected.duplicate_of_id is None
        assert dependent.duplicate_of_id is None
        assert (await session.scalars(select(TransferLink))).all() == []
    assert unrelated.duplicate_of_id == root.id
    assert root.duplicate_of_id is None
    assert stats.updated_transactions == 1 and stats.new_transactions == 0
    assert corrected.aggregator_id == "TRN-1"
    assert corrected.raw == {"id": "TRN-1", "corrected": True}
    assert (await session.scalar(select(func.count()).select_from(Transaction))) == 5
    assert (root.amount_cents, root.posted_on, root.description, root.raw) == (
        -4250,
        date(2026, 7, 1),
        "TEST",
        {},
    )
