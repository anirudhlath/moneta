"""Add accounts.source (aggregator attribution, e.g. "simplefin" / "plaid")."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("source", sa.String(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("accounts", "source")
