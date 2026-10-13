"""Retain provider identity hints and nullable overlap relationships."""

import re

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("accounts") as batch:
        batch.add_column(sa.Column("mask", sa.String(), nullable=True))
        batch.add_column(sa.Column("canonical_account_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_accounts_canonical_account_id", "accounts", ["canonical_account_id"], ["id"]
        )
    with op.batch_alter_table("transactions") as batch:
        batch.add_column(sa.Column("duplicate_of_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_transactions_duplicate_of_id", "transactions", ["duplicate_of_id"], ["id"]
        )

    # Frozen parsing: future identity heuristics must not change this migration's backfill.
    conn = op.get_bind()
    for account_id, name in conn.execute(sa.text("SELECT id, name FROM accounts")).all():
        match = re.search(r"\(([0-9]{4})\)\s*$", name)
        if match:
            conn.execute(
                sa.text("UPDATE accounts SET mask = :mask WHERE id = :id"),
                {"mask": match.group(1), "id": account_id},
            )


def downgrade() -> None:
    with op.batch_alter_table("transactions") as batch:
        batch.drop_constraint("fk_transactions_duplicate_of_id", type_="foreignkey")
        batch.drop_column("duplicate_of_id")
    with op.batch_alter_table("accounts") as batch:
        batch.drop_constraint("fk_accounts_canonical_account_id", type_="foreignkey")
        batch.drop_column("canonical_account_id")
        batch.drop_column("mask")
