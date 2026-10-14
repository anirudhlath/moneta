"""Allow several recurring series per merchant (design 2026-10-13 §3, §5)."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

# 0001 created UNIQUE(merchant, direction) unnamed, so SQLite cannot drop it by
# name. Batch-recreate the table from an explicit definition without it.
_WITHOUT_UNIQUE = sa.Table(
    "recurring_series",
    sa.MetaData(),
    sa.Column("id", sa.Integer(), primary_key=True),
    sa.Column("merchant", sa.String(), nullable=False),
    sa.Column("direction", sa.String(), nullable=False),
    sa.Column("cadence", sa.String(), nullable=False),
    sa.Column("expected_cents", sa.Integer(), nullable=False),
    sa.Column("next_expected_on", sa.Date(), nullable=False),
    sa.Column("status", sa.String(), nullable=False),
    sa.Column("discretionary", sa.Boolean(), nullable=False, server_default=sa.false()),
)


def upgrade() -> None:
    with op.batch_alter_table("recurring_series", copy_from=_WITHOUT_UNIQUE, recreate="always"):
        pass


def downgrade() -> None:
    # fails loudly (IntegrityError) if a merchant now has several series
    with op.batch_alter_table("recurring_series") as batch:
        batch.create_unique_constraint(
            "uq_recurring_series_merchant_direction", ["merchant", "direction"]
        )
