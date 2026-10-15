from datetime import date
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, inspect, select

import moneta.db
from moneta.db import init_db, make_sessionmaker
from moneta.models import Account, Holding, Transaction


def _create_frozen_0005(conn: Connection) -> None:
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(moneta.db.__file__).parent / "migrations"))
    cfg.attributes["connection"] = conn
    command.upgrade(cfg, "0005")
    conn.exec_driver_sql(
        "INSERT INTO accounts (id, aggregator_id, name, org_name, type, currency, "
        "balance_cents, balance_date, financing_mode, source) VALUES "
        "(1, 'sf-checking', 'Checking (1234)', 'Test Bank', 'checking', 'USD', "
        "123456, '2026-07-01', 0, 'simplefin'), "
        "(2, 'plaid-checking', 'Checking', 'Test Bank', 'checking', 'USD', "
        "123457, '2026-07-02', 0, 'plaid'), "
        "(3, 'sf-generic', 'Savings (joint)', 'Test Bank', 'savings', 'USD', "
        "54321, '2026-07-01', 0, 'simplefin')"
    )
    conn.exec_driver_sql(
        "INSERT INTO transactions (id, account_id, aggregator_id, posted_on, amount_cents, "
        "description, raw) VALUES "
        "(1, 1, 'sf-purchase', '2026-07-01', -4250, 'TEST PURCHASE', '{\"id\":\"sf-purchase\"}'), "
        "(2, 2, 'plaid-purchase', '2026-07-02', -4250, 'TEST PURCHASE', "
        '\'{"transaction_id":"plaid-purchase","amount":42.5}\')'
    )
    conn.exec_driver_sql(
        "INSERT INTO holdings (id, account_id, symbol, quantity, market_value_cents) "
        "VALUES (1, 3, 'TEST', 10.5, 200000)"
    )


async def test_overlap_columns_preserve_existing_observations() -> None:
    engine, sm = make_sessionmaker("sqlite+aiosqlite://")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(_create_frozen_0005)
        async with engine.connect() as conn:
            before_accounts = [
                tuple(row)
                for row in (await conn.exec_driver_sql("SELECT * FROM accounts ORDER BY id")).all()
            ]
            before_txns = [
                tuple(row)
                for row in (
                    await conn.exec_driver_sql("SELECT * FROM transactions ORDER BY id")
                ).all()
            ]
            before_holdings = (await conn.exec_driver_sql("SELECT * FROM holdings")).all()
        await init_db(engine)
        async with engine.connect() as conn:
            assert (
                await conn.exec_driver_sql("SELECT version_num FROM alembic_version")
            ).scalar() == moneta.db._HEAD
            account_rows = (await conn.exec_driver_sql("SELECT * FROM accounts ORDER BY id")).all()
            txn_rows = (await conn.exec_driver_sql("SELECT * FROM transactions ORDER BY id")).all()
            assert [
                tuple(row[: len(before_accounts[0])]) for row in account_rows
            ] == before_accounts
            assert [tuple(row[: len(before_txns[0])]) for row in txn_rows] == before_txns
            assert (await conn.exec_driver_sql("SELECT * FROM holdings")).all() == before_holdings
            account_fks = await conn.run_sync(lambda c: inspect(c).get_foreign_keys("accounts"))
            txn_fks = await conn.run_sync(lambda c: inspect(c).get_foreign_keys("transactions"))
            assert any(
                fk["constrained_columns"] == ["canonical_account_id"]
                and fk["referred_table"] == "accounts"
                and fk["referred_columns"] == ["id"]
                for fk in account_fks
            )
            assert any(
                fk["constrained_columns"] == ["duplicate_of_id"]
                and fk["referred_table"] == "transactions"
                and fk["referred_columns"] == ["id"]
                for fk in txn_fks
            )
        async with sm() as session:
            accounts = (await session.scalars(select(Account).order_by(Account.id))).all()
            assert [a.mask for a in accounts] == ["1234", None, None]
            assert [a.canonical_account_id for a in accounts] == [None, None, None]
            txns = (await session.scalars(select(Transaction).order_by(Transaction.id))).all()
            assert [t.duplicate_of_id for t in txns] == [None, None]
            assert txns[0].posted_on == date(2026, 7, 1)
            assert txns[1].raw == {"transaction_id": "plaid-purchase", "amount": 42.5}
            holding = (await session.scalars(select(Holding))).one()
            assert (holding.quantity, holding.market_value_cents) == (10.5, 200000)
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Checking (1234)", "1234"),
        ("Savings (0000) ", "0000"),
        ("Checking", None),
        ("Checking (joint)", None),
        ("Checking (12a4)", None),
        ("Checking (12345)", None),
        ("Checking (123)", None),
        ("Checking 1234", None),
        ("1234", None),
        ("Checking (１２３４)", None),
        ("Checking (1234) joint", None),
        ("", None),
    ],
)
def test_mask_parsing_is_conservative(name: str, expected: str | None) -> None:
    from moneta.identity import infer_mask

    assert infer_mask(name) == expected
