"""Add digest_state single-row table (notifications digest cursor)."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "digest_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("last_event_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("warned_account_ids", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_table("digest_state")
