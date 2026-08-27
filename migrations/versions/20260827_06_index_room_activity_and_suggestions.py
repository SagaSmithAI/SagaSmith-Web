"""index room activity and suggestion state

Revision ID: 20260827_06
Revises: 20260817_05
Create Date: 2026-08-27
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260827_06"
down_revision: Union[str, None] = "20260817_05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_BATCH_SIZE = 500


def _batches(rows: Iterable[dict[str, object]]) -> Iterable[list[dict[str, object]]]:
    batch: list[dict[str, object]] = []
    for row in rows:
        batch.append(row)
        if len(batch) == _BATCH_SIZE:
            yield batch
            batch = []
    if batch:
        yield batch


def _optional_text(value: object, limit: int) -> str | None:
    text = str(value or "")[:limit]
    return text or None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _backfill_activity_columns(bind: sa.Connection) -> None:
    events = sa.table(
        "campaign_room_events",
        sa.column("id", sa.String),
        sa.column("event_type", sa.String),
        sa.column("payload", sa.JSON),
        sa.column("run_id", sa.String),
        sa.column("activity_id", sa.String),
        sa.column("activity_state", sa.String),
    )
    statement = (
        events.update()
        .where(events.c.id == sa.bindparam("event_pk"))
        .values(
            run_id=sa.bindparam("indexed_run_id"),
            activity_id=sa.bindparam("indexed_activity_id"),
            activity_state=sa.bindparam("indexed_activity_state"),
        )
    )
    last_event_id: str | None = None
    while True:
        page = sa.select(events.c.id, events.c.payload).where(
            events.c.event_type == "room.activity"
        )
        if last_event_id is not None:
            page = page.where(events.c.id > last_event_id)
        event_rows = bind.execute(page.order_by(events.c.id).limit(_BATCH_SIZE)).all()
        if not event_rows:
            break
        updates = [
            {
                "event_pk": event_id,
                "indexed_run_id": _optional_text(payload.get("run_id"), 64),
                "indexed_activity_id": _optional_text(payload.get("activity_id"), 80),
                "indexed_activity_state": _optional_text(payload.get("state"), 24),
            }
            for event_id, payload in event_rows
            if isinstance(payload, dict)
        ]
        if updates:
            bind.execute(statement, updates)
        last_event_id = str(event_rows[-1].id)


def _backfill_suggestions(bind: sa.Connection) -> None:
    messages = sa.table(
        "campaign_messages",
        sa.column("id", sa.String),
        sa.column("room_id", sa.String),
        sa.column("sender_type", sa.String),
        sa.column("structured_payload", sa.JSON),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    suggestions = sa.table(
        "campaign_suggestions",
        sa.column("id", sa.String),
        sa.column("room_id", sa.String),
        sa.column("message_id", sa.String),
        sa.column("suggestion_id", sa.String),
        sa.column("target_user_id", sa.String),
        sa.column("actor_ref", sa.String),
        sa.column("run_id", sa.String),
        sa.column("expired", sa.Boolean),
        sa.column("valid_revision", sa.Integer),
        sa.column("valid_phase", sa.String),
        sa.column("payload", sa.JSON),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )

    def suggestion_rows(message_rows: Iterable[sa.Row]) -> Iterable[dict[str, object]]:
        for message_id, room_id, sender_type, raw_payload, created_at in message_rows:
            if (
                sender_type != "agent"
                or not isinstance(raw_payload, dict)
                or raw_payload.get("schema") != "sagasmith.room-message/v1"
            ):
                continue
            raw_suggestions = raw_payload.get("suggestions")
            if not isinstance(raw_suggestions, list):
                continue
            seen: set[str] = set()
            for raw_suggestion in raw_suggestions:
                if not isinstance(raw_suggestion, dict):
                    continue
                suggestion_id = _optional_text(raw_suggestion.get("id"), 80)
                valid_for = raw_suggestion.get("valid_for")
                if suggestion_id is None or suggestion_id in seen or not isinstance(valid_for, dict):
                    continue
                run_id = _optional_text(
                    valid_for.get("run_id") or raw_payload.get("run_id"),
                    64,
                )
                target_user_id = _optional_text(raw_suggestion.get("target_user_id"), 36)
                if run_id is None or target_user_id is None:
                    continue
                seen.add(suggestion_id)
                yield {
                    "id": str(uuid.uuid4()),
                    "room_id": room_id,
                    "message_id": message_id,
                    "suggestion_id": suggestion_id,
                    "target_user_id": target_user_id,
                    "actor_ref": _optional_text(raw_suggestion.get("actor_ref"), 64),
                    "run_id": run_id,
                    "expired": bool(valid_for.get("expired", False)),
                    "valid_revision": _optional_int(valid_for.get("revision")),
                    "valid_phase": _optional_text(valid_for.get("phase"), 64),
                    "payload": raw_suggestion,
                    "created_at": created_at,
                }

    statement = suggestions.insert()
    last_message_id: str | None = None
    while True:
        page = sa.select(
            messages.c.id,
            messages.c.room_id,
            messages.c.sender_type,
            messages.c.structured_payload,
            messages.c.created_at,
        )
        if last_message_id is not None:
            page = page.where(messages.c.id > last_message_id)
        message_rows = bind.execute(page.order_by(messages.c.id).limit(_BATCH_SIZE)).all()
        if not message_rows:
            break
        for batch in _batches(suggestion_rows(message_rows)):
            bind.execute(statement, batch)
        last_message_id = str(message_rows[-1].id)


def upgrade() -> None:
    with op.batch_alter_table("campaign_room_events") as batch_op:
        batch_op.add_column(sa.Column("run_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("activity_id", sa.String(length=80), nullable=True))
        batch_op.add_column(sa.Column("activity_state", sa.String(length=24), nullable=True))

    op.create_table(
        "campaign_suggestions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("room_id", sa.String(length=36), nullable=False),
        sa.Column("message_id", sa.String(length=36), nullable=False),
        sa.Column("suggestion_id", sa.String(length=80), nullable=False),
        sa.Column("target_user_id", sa.String(length=36), nullable=True),
        sa.Column("actor_ref", sa.String(length=64), nullable=True),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("expired", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("valid_revision", sa.Integer(), nullable=True),
        sa.Column("valid_phase", sa.String(length=64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["campaign_messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["room_id"], ["campaign_rooms.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "message_id",
            "suggestion_id",
            name="uq_campaign_suggestion_message_id",
        ),
    )
    bind = op.get_bind()
    _backfill_activity_columns(bind)
    _backfill_suggestions(bind)
    bind.execute(
        sa.text(
            "DELETE FROM campaign_suggestions "
            "WHERE target_user_id IS NOT NULL "
            "AND NOT EXISTS ("
            "SELECT 1 FROM users WHERE users.id = campaign_suggestions.target_user_id"
            ")"
        )
    )
    with op.batch_alter_table("campaign_suggestions") as batch_op:
        batch_op.create_foreign_key(
            "fk_campaign_suggestion_target_user",
            "users",
            ["target_user_id"],
            ["id"],
            ondelete="CASCADE",
        )
    op.create_index(
        "ix_campaign_suggestions_message_id",
        "campaign_suggestions",
        ["message_id"],
    )
    op.create_index(
        "ix_campaign_suggestions_room_id",
        "campaign_suggestions",
        ["room_id"],
    )
    op.create_index(
        "ix_campaign_suggestion_active_target",
        "campaign_suggestions",
        ["room_id", "target_user_id", "expired"],
    )
    op.create_index(
        "ix_campaign_room_event_activity",
        "campaign_room_events",
        ["room_id", "run_id", "activity_id", "sequence"],
    )


def downgrade() -> None:
    op.drop_index("ix_campaign_room_event_activity", table_name="campaign_room_events")
    op.drop_index("ix_campaign_suggestion_active_target", table_name="campaign_suggestions")
    op.drop_index("ix_campaign_suggestions_room_id", table_name="campaign_suggestions")
    op.drop_index("ix_campaign_suggestions_message_id", table_name="campaign_suggestions")
    op.drop_table("campaign_suggestions")
    with op.batch_alter_table("campaign_room_events") as batch_op:
        batch_op.drop_column("activity_state")
        batch_op.drop_column("activity_id")
        batch_op.drop_column("run_id")
