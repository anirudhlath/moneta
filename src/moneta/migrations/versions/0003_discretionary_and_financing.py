"""Add recurring_series.discretionary and accounts.financing_mode flags."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "recurring_series",
        sa.Column("discretionary", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "accounts",
        sa.Column("financing_mode", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("accounts", "financing_mode")
    op.drop_column("recurring_series", "discretionary")
