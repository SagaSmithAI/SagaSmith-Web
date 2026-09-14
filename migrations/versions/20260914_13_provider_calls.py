"""Durable per-attempt provider authorization and settlement state."""

import sqlalchemy as sa
from alembic import op

revision = "20260914_13"
down_revision = "20260914_12"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "provider_calls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("campaign_id", sa.String(160), nullable=False),
        sa.Column("task_id", sa.String(160), nullable=False),
        sa.Column("provider", sa.String(120), nullable=False),
        sa.Column("model", sa.String(120), nullable=False),
        sa.Column("price", sa.JSON(), nullable=False),
        sa.Column("reserved_usd", sa.Numeric(24, 6), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("settlement_hash", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_provider_calls_task_id", "provider_calls", ["task_id"])


def downgrade():
    op.drop_index("ix_provider_calls_task_id", table_name="provider_calls")
    op.drop_table("provider_calls")
