"""add uncapped provider usage and four-scope budget accounting

Revision ID: 20260914_11
Revises: 20260829_10
Create Date: 2026-09-14
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260914_11"
down_revision: Union[str, None] = "20260829_10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "provider_consumption_ledger",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=True),
        sa.Column("task_id", sa.String(length=160), nullable=True),
        sa.Column("reservation_id", sa.String(length=36), nullable=True),
        sa.Column("metric", sa.String(length=50), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=24, scale=6), nullable=False),
        sa.Column("unit", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=True),
        sa.Column("model", sa.String(length=120), nullable=True),
        sa.Column("request_id", sa.String(length=100), nullable=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("pricing_version", sa.String(length=80), nullable=True),
        sa.Column("pricing_inputs", sa.JSON(), nullable=True),
        sa.Column("pricing_cache", sa.JSON(), nullable=True),
        sa.Column("pricing_output", sa.JSON(), nullable=True),
        sa.Column("cost_amount", sa.Numeric(precision=24, scale=12), nullable=True),
        sa.Column("cost_currency", sa.String(length=12), nullable=True),
        sa.Column("cost_status", sa.String(length=24), server_default="unknown", nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("details", sa.JSON(), server_default="{}", nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_provider_consumption_idempotency"),
    )
    op.create_index(
        "ix_provider_consumption_ledger_campaign_id",
        "provider_consumption_ledger",
        ["campaign_id"],
    )
    op.create_index(
        "ix_provider_consumption_ledger_task_id",
        "provider_consumption_ledger",
        ["task_id"],
    )
    op.create_index(
        "ix_provider_consumption_ledger_request_id",
        "provider_consumption_ledger",
        ["request_id"],
    )
    op.create_index(
        "ix_provider_consumption_period",
        "provider_consumption_ledger",
        ["user_id", "metric", "occurred_at"],
    )
    op.create_table(
        "budget_limits",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("scope_type", sa.String(length=16), nullable=False),
        sa.Column("scope_id", sa.String(length=160), nullable=False),
        sa.Column("metric", sa.String(length=50), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=24, scale=6), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="active", nullable=False),
        sa.Column("source", sa.String(length=50), server_default="admin", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope_type",
            "scope_id",
            "metric",
            "period_start",
            name="uq_budget_limit_period",
        ),
    )
    op.create_index(
        "ix_budget_limit_lookup",
        "budget_limits",
        ["scope_type", "scope_id", "metric", "period_start"],
    )
    op.create_table(
        "budget_claims",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("reservation_key", sa.String(length=200), nullable=False),
        sa.Column("scope_type", sa.String(length=16), nullable=False),
        sa.Column("scope_id", sa.String(length=160), nullable=False),
        sa.Column("metric", sa.String(length=50), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=24, scale=6), nullable=False),
        sa.Column(
            "settled_quantity",
            sa.Numeric(precision=24, scale=6),
            server_default="0",
            nullable=False,
        ),
        sa.Column("unknown_reason", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="reserved", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "reservation_key",
            "scope_type",
            name="uq_budget_claim_reservation_scope",
        ),
    )
    op.create_index(
        "ix_budget_claim_lookup",
        "budget_claims",
        ["scope_type", "scope_id", "metric", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_budget_claim_lookup", table_name="budget_claims")
    op.drop_table("budget_claims")
    op.drop_index("ix_budget_limit_lookup", table_name="budget_limits")
    op.drop_table("budget_limits")
    op.drop_index("ix_provider_consumption_period", table_name="provider_consumption_ledger")
    op.drop_index(
        "ix_provider_consumption_ledger_request_id", table_name="provider_consumption_ledger"
    )
    op.drop_index(
        "ix_provider_consumption_ledger_task_id", table_name="provider_consumption_ledger"
    )
    op.drop_index(
        "ix_provider_consumption_ledger_campaign_id", table_name="provider_consumption_ledger"
    )
    op.drop_table("provider_consumption_ledger")
