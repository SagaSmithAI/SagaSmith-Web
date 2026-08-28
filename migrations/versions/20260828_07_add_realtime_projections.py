"""add realtime projection cache metadata

Revision ID: 20260828_07
Revises: 20260827_06
Create Date: 2026-08-28
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260828_07"
down_revision: Union[str, None] = "20260827_06"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("campaign_membership_projections") as batch_op:
        batch_op.add_column(
            sa.Column(
                "authorization_epoch",
                sa.Integer(),
                server_default="1",
                nullable=False,
            )
        )

    op.create_table(
        "campaign_panel_projections",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("audience_key", sa.String(length=100), nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("authorization_epoch", sa.Integer(), nullable=False),
        sa.Column("projection_schema_version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaign_projections.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "campaign_id",
            "audience_key",
            name="uq_campaign_panel_projection_audience",
        ),
    )
    op.create_index(
        "ix_campaign_panel_projections_campaign_id",
        "campaign_panel_projections",
        ["campaign_id"],
    )
    op.create_index(
        "ix_campaign_panel_projection_freshness",
        "campaign_panel_projections",
        ["campaign_id", "audience_key", "source_revision", "authorization_epoch"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_campaign_panel_projection_freshness",
        table_name="campaign_panel_projections",
    )
    op.drop_index(
        "ix_campaign_panel_projections_campaign_id",
        table_name="campaign_panel_projections",
    )
    op.drop_table("campaign_panel_projections")
    with op.batch_alter_table("campaign_membership_projections") as batch_op:
        batch_op.drop_column("authorization_epoch")
