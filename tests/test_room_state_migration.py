from __future__ import annotations

from datetime import UTC, datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import sqlalchemy as sa

from sagasmith_service.database import make_engine


def _migration_module():
    path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "20260827_06_index_room_activity_and_suggestions.py"
    )
    spec = spec_from_file_location("room_state_migration", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_room_state_migration_backfills_structured_indexes() -> None:
    metadata = sa.MetaData()
    events = sa.Table(
        "campaign_room_events",
        metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("event_type", sa.String, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("run_id", sa.String),
        sa.Column("activity_id", sa.String),
        sa.Column("activity_state", sa.String),
    )
    messages = sa.Table(
        "campaign_messages",
        metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("room_id", sa.String, nullable=False),
        sa.Column("sender_type", sa.String, nullable=False),
        sa.Column("structured_payload", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    suggestions = sa.Table(
        "campaign_suggestions",
        metadata,
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("room_id", sa.String, nullable=False),
        sa.Column("message_id", sa.String, nullable=False),
        sa.Column("suggestion_id", sa.String, nullable=False),
        sa.Column("target_user_id", sa.String),
        sa.Column("actor_ref", sa.String),
        sa.Column("run_id", sa.String, nullable=False),
        sa.Column("expired", sa.Boolean, nullable=False),
        sa.Column("valid_revision", sa.Integer),
        sa.Column("valid_phase", sa.String),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    engine = make_engine("sqlite://")
    metadata.create_all(engine)
    created_at = datetime.now(UTC)
    structured_payload = {
        "schema": "sagasmith.room-message/v1",
        "run_id": "run-1",
        "suggestions": [
            {
                "id": "suggestion-1",
                "text": "Continue",
                "target_user_id": "user-1",
                "actor_ref": "actor-1",
                "valid_for": {
                    "run_id": "run-1",
                    "revision": "7",
                    "phase": "play",
                    "expired": False,
                },
            }
        ],
    }
    with engine.begin() as connection:
        connection.execute(
            events.insert(),
            {
                "id": "event-1",
                "event_type": "room.activity",
                "payload": {
                    "run_id": "run-1",
                    "activity_id": "activity-1",
                    "state": "started",
                },
            },
        )
        connection.execute(
            messages.insert(),
            [
                {
                    "id": "message-1",
                    "room_id": "room-1",
                    "sender_type": "agent",
                    "structured_payload": structured_payload,
                    "created_at": created_at,
                },
                {
                    "id": "message-2",
                    "room_id": "room-1",
                    "sender_type": "user",
                    "structured_payload": structured_payload,
                    "created_at": created_at,
                },
            ],
        )
        migration = _migration_module()
        migration._backfill_activity_columns(connection)
        migration._backfill_suggestions(connection)

        activity = connection.execute(
            sa.select(events.c.run_id, events.c.activity_id, events.c.activity_state)
        ).one()
        suggestion = connection.execute(sa.select(suggestions)).mappings().one()

    assert tuple(activity) == ("run-1", "activity-1", "started")
    assert suggestion["message_id"] == "message-1"
    assert suggestion["target_user_id"] == "user-1"
    assert suggestion["actor_ref"] == "actor-1"
    assert suggestion["run_id"] == "run-1"
    assert suggestion["expired"] is False
    assert suggestion["valid_revision"] == 7
    assert suggestion["valid_phase"] == "play"
