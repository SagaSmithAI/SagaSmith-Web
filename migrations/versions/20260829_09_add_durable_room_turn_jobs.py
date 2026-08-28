"""add durable room turn jobs and hosted media artifacts

Revision ID: 20260829_09
Revises: 20260828_08
Create Date: 2026-08-29
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260829_09"
down_revision: Union[str, None] = "20260828_08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "room_turn_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("room_id", sa.String(length=36), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("trigger_message_id", sa.String(length=36), nullable=False),
        sa.Column("agent_run_id", sa.String(length=36), nullable=True),
        sa.Column("reservation_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=24), server_default="queued", nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("base_revision", sa.Integer(), nullable=True),
        sa.Column("result_revision", sa.Integer(), nullable=True),
        sa.Column("result_message_ids", sa.JSON(), nullable=False),
        sa.Column("agent_result", sa.JSON(), nullable=False),
        sa.Column("authority_context", sa.JSON(), nullable=False),
        sa.Column("trace_context", sa.JSON(), nullable=False),
        sa.Column("attempt", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=100), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("retryable", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("error_class", sa.String(length=50), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("last_error", sa.String(length=1000), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaign_projections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reservation_id"], ["quota_reservations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["room_id"], ["campaign_rooms.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["trigger_message_id"], ["campaign_messages.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_run_id"),
        sa.UniqueConstraint("trigger_message_id"),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_room_turn_job_retry"),
    )
    op.create_index("ix_room_turn_jobs_room_id", "room_turn_jobs", ["room_id"])
    op.create_index("ix_room_turn_jobs_campaign_id", "room_turn_jobs", ["campaign_id"])
    op.create_index("ix_room_turn_jobs_user_id", "room_turn_jobs", ["user_id"])
    op.create_index(
        "ix_room_turn_jobs_trigger_message_id", "room_turn_jobs", ["trigger_message_id"]
    )
    op.create_index("ix_room_turn_jobs_agent_run_id", "room_turn_jobs", ["agent_run_id"])
    op.create_index("ix_room_turn_jobs_reservation_id", "room_turn_jobs", ["reservation_id"])
    op.create_index("ix_room_turn_jobs_status", "room_turn_jobs", ["status"])
    op.create_index("ix_room_turn_jobs_lease_owner", "room_turn_jobs", ["lease_owner"])
    op.create_index("ix_room_turn_jobs_lease_expires_at", "room_turn_jobs", ["lease_expires_at"])
    op.create_index(
        "ix_room_turn_job_queue", "room_turn_jobs", ["status", "available_at", "created_at"]
    )
    op.create_index("ix_room_turn_job_room", "room_turn_jobs", ["room_id", "created_at"])

    op.create_table(
        "room_media_artifacts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("room_id", sa.String(length=36), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("content_index", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("media_type", sa.String(length=160), nullable=False),
        sa.Column("storage_key", sa.String(length=500), nullable=True),
        sa.Column("resource_uri", sa.String(length=1000), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("size_bytes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("audience", sa.String(length=24), server_default="public", nullable=False),
        sa.Column("audience_user_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaign_projections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["room_turn_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["room_id"], ["campaign_rooms.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "content_index", name="uq_room_media_job_content"),
        sa.UniqueConstraint("storage_key"),
    )
    op.create_index("ix_room_media_artifacts_job_id", "room_media_artifacts", ["job_id"])
    op.create_index("ix_room_media_artifacts_room_id", "room_media_artifacts", ["room_id"])
    op.create_index("ix_room_media_artifacts_campaign_id", "room_media_artifacts", ["campaign_id"])
    op.create_index("ix_room_media_artifacts_audience", "room_media_artifacts", ["audience"])
    op.create_index("ix_room_media_room_created", "room_media_artifacts", ["room_id", "created_at"])


def downgrade() -> None:
    op.drop_table("room_media_artifacts")
    op.drop_table("room_turn_jobs")
