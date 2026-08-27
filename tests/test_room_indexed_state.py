from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from sagasmith_service.api.rooms import (
    _append_message,
    _close_run_activities,
    _expire_suggestions,
)
from sagasmith_service.models import (
    CampaignMessage,
    CampaignRoom,
    CampaignRoomEvent,
    CampaignSuggestion,
)

PASSWORD = "correct horse battery staple"


def _room_fixture(client: TestClient) -> tuple[str, str]:
    registered = client.post(
        "/api/auth/register",
        json={
            "email": "indexed-room@example.com",
            "password": PASSWORD,
            "display_name": "Indexed Room",
        },
    )
    assert registered.status_code == 201
    created = client.post(
        "/api/campaigns",
        headers={"Idempotency-Key": "indexed-room-campaign"},
        json={"name": "Indexed Room Campaign"},
    )
    assert created.status_code == 201
    room = client.get("/api/campaigns/campaign-1/room")
    assert room.status_code == 200
    return room.json()["id"], registered.json()["user"]["id"]


@contextmanager
def _capture_sql(client: TestClient) -> Iterator[list[str]]:
    statements: list[str] = []

    def collect(_connection, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(statement.lower())

    event.listen(client.app.state.engine, "before_cursor_execute", collect)
    try:
        yield statements
    finally:
        event.remove(client.app.state.engine, "before_cursor_execute", collect)


def test_closing_activity_uses_run_index_instead_of_room_history(client: TestClient) -> None:
    room_id, _ = _room_fixture(client)
    with client.app.state.session_factory() as session:
        room = session.get(CampaignRoom, room_id)
        assert room is not None
        first_sequence = room.next_event_sequence
        history = [
            CampaignRoomEvent(
                room_id=room.id,
                sequence=first_sequence + index,
                event_type="room.activity",
                run_id=f"historical-run-{index}",
                activity_id="historical-activity",
                activity_state="completed",
                payload={
                    "run_id": f"historical-run-{index}",
                    "activity_id": "historical-activity",
                    "state": "completed",
                },
            )
            for index in range(500)
        ]
        target = CampaignRoomEvent(
            room_id=room.id,
            sequence=first_sequence + len(history),
            event_type="room.activity",
            run_id="target-run",
            activity_id="target-activity",
            activity_state="started",
            payload={
                "run_id": "target-run",
                "activity_id": "target-activity",
                "state": "started",
                "code": "reviewing_rules",
                "audience": "private",
                "audience_user_ids": [],
            },
        )
        session.add_all([*history, target])
        room.next_event_sequence += len(history) + 1
        session.commit()

    with client.app.state.engine.connect() as connection:
        plan = connection.exec_driver_sql(
            "EXPLAIN QUERY PLAN SELECT event.activity_id, event.activity_state, event.payload "
            "FROM campaign_room_events AS event JOIN ("
            "SELECT activity_id, MAX(sequence) AS sequence FROM campaign_room_events "
            "WHERE room_id = ? AND run_id = ? AND activity_id IS NOT NULL "
            "GROUP BY activity_id"
            ") AS latest ON event.activity_id = latest.activity_id "
            "AND event.sequence = latest.sequence "
            "WHERE event.room_id = ? AND event.run_id = ?",
            (room_id, "target-run", room_id, "target-run"),
        ).all()
    assert any("ix_campaign_room_event_activity" in str(row[-1]) for row in plan)

    with _capture_sql(client) as statements:
        with client.app.state.session_factory() as session:
            room = session.get(CampaignRoom, room_id)
            assert room is not None
            _close_run_activities(
                session,
                room,
                run_id="target-run",
                state="superseded",
            )
            session.commit()

    activity_selects = [
        statement
        for statement in statements
        if statement.lstrip().startswith("select")
        and "campaign_room_events.activity_id" in statement
    ]
    assert len(activity_selects) == 1
    assert "campaign_room_events.run_id" in activity_selects[0]
    with client.app.state.session_factory() as session:
        target_states = session.scalars(
            select(CampaignRoomEvent.activity_state)
            .where(
                CampaignRoomEvent.room_id == room_id,
                CampaignRoomEvent.run_id == "target-run",
                CampaignRoomEvent.activity_id == "target-activity",
            )
            .order_by(CampaignRoomEvent.sequence)
        ).all()
    assert target_states == ["started", "superseded"]


def test_expiring_suggestions_loads_only_active_suggestion_messages(client: TestClient) -> None:
    room_id, user_id = _room_fixture(client)
    with client.app.state.session_factory() as session:
        room = session.get(CampaignRoom, room_id)
        assert room is not None
        first_sequence = room.next_message_sequence
        session.add_all(
            [
                CampaignMessage(
                    room_id=room.id,
                    campaign_id="campaign-1",
                    sequence=first_sequence + index,
                    sender_type="system",
                    sender_display_name="History",
                    message_type="chat",
                    audience="public",
                    content=f"history-{index}",
                    client_message_id=f"history-{index}",
                )
                for index in range(500)
            ]
        )
        room.next_message_sequence += 500
        target = _append_message(
            session,
            room,
            campaign_id="campaign-1",
            sender_type="agent",
            sender_display_name="SagaSmith",
            message_type="presentation",
            audience="public",
            content="Choose.",
            client_message_id="indexed-suggestion",
            structured_payload={
                "schema": "sagasmith.room-message/v1",
                "run_id": "suggestion-run",
                "blocks": [],
                "suggestions": [
                    {
                        "id": "suggestion-1",
                        "text": "Continue",
                        "target_user_id": user_id,
                        "valid_for": {
                            "run_id": "suggestion-run",
                            "revision": 7,
                            "phase": "play",
                            "expired": False,
                        },
                    }
                ],
            },
        )
        target_id = target.id
        session.commit()

    with client.app.state.engine.connect() as connection:
        plan = connection.exec_driver_sql(
            "EXPLAIN QUERY PLAN SELECT id FROM campaign_suggestions "
            "WHERE room_id = ? AND target_user_id = ? AND expired = 0",
            (room_id, user_id),
        ).all()
    assert any("ix_campaign_suggestion_active_target" in str(row[-1]) for row in plan)

    with _capture_sql(client) as statements:
        with client.app.state.session_factory() as session:
            room = session.get(CampaignRoom, room_id)
            assert room is not None
            _expire_suggestions(session, room, target_user_id=user_id)
            session.commit()

    message_selects = [
        statement
        for statement in statements
        if statement.lstrip().startswith("select") and "from campaign_messages" in statement
    ]
    assert len(message_selects) == 1
    assert "campaign_messages.id in" in message_selects[0]
    assert "campaign_messages.room_id =" not in message_selects[0]
    with client.app.state.session_factory() as session:
        suggestion = session.scalar(
            select(CampaignSuggestion).where(
                CampaignSuggestion.message_id == target_id,
                CampaignSuggestion.suggestion_id == "suggestion-1",
            )
        )
        message = session.get(CampaignMessage, target_id)
        assert suggestion is not None
        assert message is not None
        assert suggestion.expired is True
        assert suggestion.payload["valid_for"]["expired"] is True
        assert message.structured_payload["suggestions"][0]["valid_for"]["expired"] is True
        assert session.scalar(
            select(func.count())
            .select_from(CampaignMessage)
            .where(CampaignMessage.room_id == room_id)
        ) == 501
