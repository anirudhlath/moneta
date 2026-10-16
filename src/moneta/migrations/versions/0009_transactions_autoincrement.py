"""Never reuse a transaction id: rebuild transactions with AUTOINCREMENT.

Without AUTOINCREMENT SQLite hands the highest deleted id to the next insert, and
review payloads name transactions by id. The batch copy keeps every id, and SQLite
sets sqlite_sequence to the highest id copied, so new ids land above every id in the
table. An id reused before this migration cannot be detected.
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite cannot add AUTOINCREMENT in place. Reflection does not report it either,
    # so any later batch rebuild of this table must pass the same table_kwargs.
    # The rebuild drops a table that transfer_links and transactions itself
    # reference, which is safe only while PRAGMA foreign_keys is off (the app's
    # default: db.py never turns it on). init_db runs this inside one transaction.
    with op.batch_alter_table(
        "transactions", recreate="always", table_kwargs={"sqlite_autoincrement": True}
    ):
        pass


def downgrade() -> None:
    with op.batch_alter_table(
        "transactions", recreate="always", table_kwargs={"sqlite_autoincrement": False}
    ):
        pass
