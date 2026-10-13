from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from moneta.logical import load_logical_data, resolve_account_roots
from moneta.models import Account, AccountType, Holding, Transaction
from tests.factories import make_account, make_txn


async def test_logical_accounts_choose_one_fresh_balance(session: AsyncSession) -> None:
    root = await make_account(
        session,
        source="plaid",
        name="Reviewed checking",
        type=AccountType.checking,
        financing_mode=True,
        promo_expires_on=date(2027, 1, 1),
        balance_cents=100000,
        balance_date=date(2026, 7, 1),
    )
    other = await make_account(
        session,
        source="simplefin",
        canonical_account_id=root.id,
        type=AccountType.unknown,
        balance_cents=120000,
        balance_date=date(2026, 7, 2),
    )

    data = await load_logical_data(session)

    assert list(data.accounts) == [root.id]
    account = data.accounts[root.id]
    assert account.balance_cents == 120000
    assert account.balance_date == date(2026, 7, 2)
    assert account.name == "Reviewed checking"
    assert account.type == AccountType.checking
    assert account.financing_mode is True
    assert account.promo_expires_on == date(2027, 1, 1)
    assert data.account_roots == {root.id: root.id, other.id: root.id}
    assert root.balance_cents == 100000
    assert other.balance_cents == 120000


async def test_balance_date_ties_prefer_root_and_then_stable_source(
    session: AsyncSession,
) -> None:
    root = await make_account(session, source="root", balance_cents=100000)
    later_id = await make_account(
        session, source="z", canonical_account_id=root.id, balance_cents=200000
    )
    alphabetic = await make_account(
        session, source="a", canonical_account_id=root.id, balance_cents=300000
    )
    assert (await load_logical_data(session)).accounts[root.id].balance_cents == 100000

    later_id.balance_date = alphabetic.balance_date = date(2026, 7, 2)
    assert (await load_logical_data(session)).accounts[root.id].balance_cents == 300000


async def test_account_roots_flatten_confirmed_chains(session: AsyncSession) -> None:
    root = await make_account(session)
    middle = await make_account(session, canonical_account_id=root.id)
    leaf = await make_account(session, canonical_account_id=middle.id)
    assert resolve_account_roots([leaf, middle, root]) == {
        root.id: root.id,
        middle.id: root.id,
        leaf.id: root.id,
    }


@pytest.mark.parametrize("relationship", ["dangling", "cycle", "self"])
async def test_rejects_invalid_account_relationships(
    session: AsyncSession, relationship: str
) -> None:
    first = await make_account(session)
    second = await make_account(session, canonical_account_id=first.id)
    first.canonical_account_id = {
        "dangling": 999999,
        "cycle": second.id,
        "self": first.id,
    }[relationship]
    with pytest.raises(ValueError, match="account"):
        resolve_account_roots([first, second])


async def test_logical_survivors_keep_older_and_unique_rows(session: AsyncSession) -> None:
    root = await make_account(session, source="simplefin")
    older_source = await make_account(session, source="plaid", canonical_account_id=root.id)
    old = await make_txn(session, older_source, posted_on=date(2025, 7, 1))
    survivor = await make_txn(session, root, amount_cents=-2500, description="COFFEE")
    duplicate = await make_txn(
        session, older_source, amount_cents=-2500, duplicate_of_id=survivor.id
    )
    repeated = await make_txn(session, root, amount_cents=-2500, description="COFFEE")
    unrelated = await make_account(session, source="plaid")
    unique = await make_txn(session, unrelated, amount_cents=-2500, description="COFFEE")

    data = await load_logical_data(session)

    assert [txn.id for txn in data.transactions] == [old.id, survivor.id, repeated.id, unique.id]
    assert data.transaction_roots == {
        old.id: old.id,
        survivor.id: survivor.id,
        duplicate.id: survivor.id,
        repeated.id: repeated.id,
        unique.id: unique.id,
    }
    assert duplicate.account_id == older_source.id
    assert duplicate.amount_cents == -2500
    assert duplicate.duplicate_of_id == survivor.id


async def test_transaction_chains_resolve_to_live_survivor(session: AsyncSession) -> None:
    root = await make_account(session, source="a")
    second = await make_account(session, source="b", canonical_account_id=root.id)
    third = await make_account(session, source="c", canonical_account_id=second.id)
    survivor = await make_txn(session, root)
    middle = await make_txn(session, second, duplicate_of_id=survivor.id)
    leaf = await make_txn(session, third, duplicate_of_id=middle.id)
    data = await load_logical_data(session)
    assert [txn.id for txn in data.transactions] == [survivor.id]
    assert data.transaction_roots == {
        survivor.id: survivor.id,
        middle.id: survivor.id,
        leaf.id: survivor.id,
    }


@pytest.mark.parametrize(
    "relationship", ["dangling", "cycle", "self", "different_account", "same_source"]
)
async def test_rejects_invalid_transaction_relationships(
    session: AsyncSession, relationship: str
) -> None:
    root = await make_account(session, source="a")
    other = await make_account(
        session,
        source="a" if relationship == "same_source" else "b",
        canonical_account_id=None if relationship == "different_account" else root.id,
    )
    first = await make_txn(session, root)
    second = await make_txn(session, other, duplicate_of_id=first.id)
    if relationship in {"dangling", "cycle", "self"}:
        first.duplicate_of_id = {
            "dangling": 999999,
            "cycle": second.id,
            "self": first.id,
        }[relationship]
    with pytest.raises(ValueError, match="transaction"):
        await load_logical_data(session)


async def test_duplicate_group_cannot_collapse_same_provider_purchases(
    session: AsyncSession,
) -> None:
    root = await make_account(session, source="a")
    other = await make_account(session, source="b", canonical_account_id=root.id)
    survivor = await make_txn(session, root)
    await make_txn(session, other, duplicate_of_id=survivor.id)
    await make_txn(session, other, duplicate_of_id=survivor.id)
    with pytest.raises(ValueError, match="transaction"):
        await load_logical_data(session)


async def test_holdings_fallback_preserves_vesting(session: AsyncSession) -> None:
    root = await make_account(session, source="plaid", type=AccountType.brokerage)
    other = await make_account(
        session,
        source="simplefin",
        canonical_account_id=root.id,
        balance_date=date(2026, 7, 2),
    )
    holding = Holding(
        account_id=root.id,
        symbol="TEST",
        quantity=10.0,
        market_value_cents=100000,
        vested_quantity=3.0,
        unvested_quantity=7.0,
    )
    session.add(holding)
    await session.flush()
    data = await load_logical_data(session)
    assert len(data.holdings) == 1
    assert data.holdings[0].account_id == root.id
    assert data.holdings[0].quantity == 10.0
    assert data.holdings[0].vested_quantity == 3.0
    assert data.holdings[0].unvested_quantity == 7.0

    session.add(
        Holding(account_id=other.id, symbol="TEST", quantity=12.0, market_value_cents=120000)
    )
    await session.flush()
    data = await load_logical_data(session)
    assert len(data.holdings) == 1
    assert data.holdings[0].quantity == 12.0
    assert data.holdings[0].market_value_cents == 120000
    assert data.holdings[0].vested_quantity == 3.0
    assert data.holdings[0].unvested_quantity == 7.0
    assert holding.quantity == 10.0
    assert holding.market_value_cents == 100000


async def test_holdings_choose_one_set_without_merging_stale_symbols(
    session: AsyncSession,
) -> None:
    root = await make_account(session, source="plaid")
    other = await make_account(
        session,
        source="simplefin",
        canonical_account_id=root.id,
        balance_date=date(2026, 7, 2),
    )
    session.add_all(
        [
            Holding(account_id=root.id, symbol="SOLD", quantity=5, market_value_cents=1000),
            Holding(account_id=other.id, symbol="NEW", quantity=2, market_value_cents=2000),
        ]
    )
    await session.flush()
    data = await load_logical_data(session)
    assert [(row.symbol, row.quantity, row.market_value_cents) for row in data.holdings] == [
        ("NEW", 2.0, 2000)
    ]


async def test_conflicting_root_vesting_requires_review_without_replacing_quantities(
    session: AsyncSession,
) -> None:
    root = await make_account(session, source="plaid")
    other = await make_account(
        session,
        source="simplefin",
        canonical_account_id=root.id,
        balance_date=date(2026, 7, 2),
    )
    root_holding = Holding(
        account_id=root.id,
        symbol="TEST",
        quantity=10,
        market_value_cents=100000,
        vested_quantity=0,
        unvested_quantity=10,
    )
    other_holding = Holding(
        account_id=other.id,
        symbol="TEST",
        quantity=12,
        market_value_cents=120000,
        vested_quantity=4,
        unvested_quantity=8,
    )
    session.add_all([root_holding, other_holding])
    await session.flush()
    data = await load_logical_data(session)
    assert (data.holdings[0].vested_quantity, data.holdings[0].unvested_quantity) == (0, 10)
    assert len(data.warnings) == 1
    assert "TEST" in data.warnings[0]
    assert "review" in data.warnings[0].lower()
    assert (root_holding.vested_quantity, root_holding.unvested_quantity) == (0, 10)
    assert (other_holding.vested_quantity, other_holding.unvested_quantity) == (4, 8)


async def test_unambiguous_nonroot_vesting_follows_selected_holdings(
    session: AsyncSession,
) -> None:
    root = await make_account(session, source="plaid")
    other = await make_account(session, source="simplefin", canonical_account_id=root.id)
    session.add(
        Holding(
            account_id=other.id,
            symbol="TEST",
            quantity=12,
            market_value_cents=120000,
            vested_quantity=4,
            unvested_quantity=8,
        )
    )
    await session.flush()
    data = await load_logical_data(session)
    assert (data.holdings[0].vested_quantity, data.holdings[0].unvested_quantity) == (4, 8)
    assert data.warnings == []


async def test_projection_does_not_flush_or_commit_source_facts(session: AsyncSession) -> None:
    root = await make_account(session, source="plaid", balance_cents=100000)
    await session.commit()
    txn = await make_txn(session, root, raw={"provider": "unchanged"})
    root.name = "Pending metadata"
    pending = Account(name="Not yet stored", balance_date=date(2026, 7, 1), aggregator_id="NEW")
    session.add(pending)
    data = await load_logical_data(session)
    assert data.accounts[root.id].name == "Pending metadata"
    assert pending in session.new
    assert root in session.dirty
    assert txn.raw == {"provider": "unchanged"}
    await session.rollback()
    assert list(await session.scalars(select(Transaction))) == []


async def test_conflicting_alias_vesting_stays_unknown_until_review(
    session: AsyncSession,
) -> None:
    root = await make_account(session, source="root")
    first = await make_account(session, source="a", canonical_account_id=root.id)
    second = await make_account(session, source="b", canonical_account_id=root.id)
    session.add_all(
        [
            Holding(
                account_id=first.id,
                symbol="TEST",
                quantity=10,
                market_value_cents=100000,
                vested_quantity=3,
                unvested_quantity=7,
            ),
            Holding(
                account_id=second.id,
                symbol="TEST",
                quantity=10,
                market_value_cents=100000,
                vested_quantity=4,
                unvested_quantity=6,
            ),
        ]
    )
    await session.flush()
    data = await load_logical_data(session)
    assert (data.holdings[0].vested_quantity, data.holdings[0].unvested_quantity) == (None, None)
    assert len(data.warnings) == 1
    assert "review" in data.warnings[0].lower()


async def test_empty_projection(session: AsyncSession) -> None:
    data = await load_logical_data(session)
    assert data.accounts == {}
    assert data.account_roots == {}
    assert data.transactions == []
    assert data.transaction_roots == {}
    assert data.holdings == []
    assert data.warnings == []
