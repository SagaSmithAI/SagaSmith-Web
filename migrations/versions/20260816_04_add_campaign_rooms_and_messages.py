"""add campaign rooms, messages and realtime outbox

Revision ID: 20260816_04
Revises: c5f887f6f8ac
Create Date: 2026-08-16
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260816_04"
down_revision: Union[str, None] = "c5f887f6f8ac"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "campaign_rooms",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("next_message_sequence", sa.Integer(), nullable=False),
        sa.Column("next_event_sequence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaign_projections.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id"),
    )
    op.create_index("ix_campaign_rooms_campaign_id", "campaign_rooms", ["campaign_id"])
    op.create_index("ix_campaign_rooms_status", "campaign_rooms", ["status"])

    op.create_table(
        "campaign_messages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("room_id", sa.String(length=36), nullable=False),
        sa.Column("campaign_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("sender_type", sa.String(length=24), nullable=False),
        sa.Column("sender_user_id", sa.String(length=36), nullable=True),
        sa.Column("sender_display_name", sa.String(length=160), nullable=False),
        sa.Column("message_type", sa.String(length=32), nullable=False),
        sa.Column("audience", sa.String(length=24), nullable=False),
        sa.Column("audience_user_ids", sa.JSON(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("structured_payload", sa.JSON(), nullable=False),
        sa.Column("reply_to_message_id", sa.String(length=36), nullable=True),
        sa.Column("trigger_message_id", sa.String(length=36), nullable=True),
        sa.Column("mcp_revision", sa.Integer(), nullable=True),
        sa.Column("mcp_receipt", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("client_message_id", sa.String(length=160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaign_projections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reply_to_message_id"], ["campaign_messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["room_id"], ["campaign_rooms.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sender_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["trigger_message_id"], ["campaign_messages.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("room_id", "client_message_id", name="uq_campaign_message_retry"),
        sa.UniqueConstraint("room_id", "sequence", name="uq_campaign_message_sequence"),
    )
    for column in (
        "campaign_id",
        "message_type",
        "reply_to_message_id",
        "room_id",
        "sender_type",
        "sender_user_id",
        "status",
        "trigger_message_id",
    ):
        op.create_index(f"ix_campaign_messages_{column}", "campaign_messages", [column])
    op.create_index(
        "ix_campaign_message_timeline", "campaign_messages", ["room_id", "sequence"]
    )

    op.create_table(
        "campaign_room_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("room_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["room_id"], ["campaign_rooms.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("room_id", "sequence", name="uq_campaign_room_event_sequence"),
    )
    op.create_index("ix_campaign_room_events_event_type", "campaign_room_events", ["event_type"])
    op.create_index("ix_campaign_room_events_room_id", "campaign_room_events", ["room_id"])
    op.create_index(
        "ix_campaign_room_event_stream", "campaign_room_events", ["room_id", "sequence"]
    )

    op.create_table(
        "campaign_room_read_cursors",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("room_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("last_read_sequence", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["room_id"], ["campaign_rooms.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("room_id", "user_id", name="uq_campaign_room_reader"),
    )
    op.create_index(
        "ix_campaign_room_read_cursors_room_id", "campaign_room_read_cursors", ["room_id"]
    )
    op.create_index(
        "ix_campaign_room_read_cursors_user_id", "campaign_room_read_cursors", ["user_id"]
    )

    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.add_column(sa.Column("trigger_message_id", sa.String(length=36), nullable=True))
        batch_op.create_index("ix_agent_runs_trigger_message_id", ["trigger_message_id"])
        batch_op.create_foreign_key(
            "fk_agent_run_trigger_message",
            "campaign_messages",
            ["trigger_message_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("agent_runs") as batch_op:
        batch_op.drop_constraint("fk_agent_run_trigger_message", type_="foreignkey")
        batch_op.drop_index("ix_agent_runs_trigger_message_id")
        batch_op.drop_column("trigger_message_id")
    op.drop_index("ix_campaign_room_read_cursors_user_id", table_name="campaign_room_read_cursors")
    op.drop_index("ix_campaign_room_read_cursors_room_id", table_name="campaign_room_read_cursors")
    op.drop_table("campaign_room_read_cursors")
    op.drop_index("ix_campaign_room_event_stream", table_name="campaign_room_events")
    op.drop_index("ix_campaign_room_events_room_id", table_name="campaign_room_events")
    op.drop_index("ix_campaign_room_events_event_type", table_name="campaign_room_events")
    op.drop_table("campaign_room_events")
    op.drop_index("ix_campaign_message_timeline", table_name="campaign_messages")
    for column in reversed(
        (
            "campaign_id",
            "message_type",
            "reply_to_message_id",
            "room_id",
            "sender_type",
            "sender_user_id",
            "status",
            "trigger_message_id",
        )
    ):
        op.drop_index(f"ix_campaign_messages_{column}", table_name="campaign_messages")
    op.drop_table("campaign_messages")
    op.drop_index("ix_campaign_rooms_status", table_name="campaign_rooms")
    op.drop_index("ix_campaign_rooms_campaign_id", table_name="campaign_rooms")
    op.drop_table("campaign_rooms")
