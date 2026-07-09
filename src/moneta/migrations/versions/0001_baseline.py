"""Baseline: schema as of 2026-07-09 (the pre-migration create_all schema)."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("aggregator_id", sa.String(), nullable=False, unique=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("org_name", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("balance_cents", sa.Integer(), nullable=False),
        sa.Column("balance_date", sa.Date(), nullable=False),
        sa.Column("promo_expires_on", sa.Date(), nullable=True),
    )
    op.create_table(
        "recurring_series",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("merchant", sa.String(), nullable=False),
        sa.Column("direction", sa.String(), nullable=False),
        sa.Column("cadence", sa.String(), nullable=False),
        sa.Column("expected_cents", sa.Integer(), nullable=False),
        sa.Column("next_expected_on", sa.Date(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.UniqueConstraint("merchant", "direction"),
    )
    op.create_table(
        "transactions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("aggregator_id", sa.String(), nullable=False),
        sa.Column("posted_on", sa.Date(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("merchant", sa.String(), nullable=True),
        sa.Column("series_id", sa.Integer(), sa.ForeignKey("recurring_series.id"), nullable=True),
        sa.Column("raw", sa.JSON(), nullable=False),
        sa.UniqueConstraint("account_id", "aggregator_id"),
    )
    op.create_table(
        "transfer_links",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "outflow_id",
            sa.Integer(),
            sa.ForeignKey("transactions.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "inflow_id",
            sa.Integer(),
            sa.ForeignKey("transactions.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("method", sa.String(), nullable=False),
    )
    op.create_table(
        "series_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("series_id", sa.Integer(), sa.ForeignKey("recurring_series.id"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("occurred_on", sa.Date(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
    )
    op.create_table(
        "holdings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("market_value_cents", sa.Integer(), nullable=False),
        sa.Column("vested_quantity", sa.Float(), nullable=True),
        sa.Column("unvested_quantity", sa.Float(), nullable=True),
        sa.UniqueConstraint("account_id", "symbol"),
    )
    op.create_table(
        "merchant_aliases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("raw_descriptor", sa.String(), nullable=False, unique=True),
        sa.Column("merchant", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
    )
    op.create_table(
        "review_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("question", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("resolution", sa.JSON(), nullable=True),
        sa.Column(
            "created_on",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    for table in (
        "review_items",
        "merchant_aliases",
        "holdings",
        "series_events",
        "transfer_links",
        "transactions",
        "recurring_series",
        "accounts",
    ):
        op.drop_table(table)
