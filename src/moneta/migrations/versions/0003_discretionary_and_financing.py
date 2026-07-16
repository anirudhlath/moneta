"""Add recurring_series.discretionary and accounts.financing_mode flags."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: test_init_db_adopts_pre_migration_database simulates the
    # pre-Alembic DB from current Base.metadata (minus only brand-new tables),
    # so a fixture built against post-0003 models already carries these
    # columns; skip rather than error if a column is already present.
    inspector = sa.inspect(op.get_bind())
    rs_columns = {col["name"] for col in inspector.get_columns("recurring_series")}
    if "discretionary" not in rs_columns:
        op.add_column(
            "recurring_series",
            sa.Column("discretionary", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    accounts_columns = {col["name"] for col in inspector.get_columns("accounts")}
    if "financing_mode" not in accounts_columns:
        op.add_column(
            "accounts",
            sa.Column("financing_mode", sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    op.drop_column("accounts", "financing_mode")
    op.drop_column("recurring_series", "discretionary")
