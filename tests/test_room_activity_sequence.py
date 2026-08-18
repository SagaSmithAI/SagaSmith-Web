from typing import Any

from conftest import FakeAgentRuntime
from fastapi.testclient import TestClient
from sqlalchemy import select

from sagasmith_service.api.rooms import _emit
from sagasmith_service.models import CampaignRoom, CampaignRoomEvent

PASSWORD = "correct horse battery staple"


def _register_and_create_campaign(client: TestClient) -> None:
    registered = client.post(
        "/api/auth/register",
        json={
            "email": "live-activity@example.com",
            "password": PASSWORD,
            "display_name": "Live Activity",
        },
    )
    assert registered.status_code == 201
    created = client.post(
        "/api/campaigns",
        headers={"Idempotency-Key": "live-activity-campaign"},
        json={"name": "Live Activity Campaign"},
    )
    assert created.status_code == 201


def test_agent_failure_refreshes_sequence_after_live_activity_callback(
    client: TestClient,
    agent_runtime: FakeAgentRuntime,
) -> None:
    _register_and_create_campaign(client)

    def invalid_output(context: dict[str, Any]) -> dict[str, Any]:
        with client.app.state.session_factory() as callback_session:
            room = callback_session.scalar(
                select(CampaignRoom)
                .where(CampaignRoom.campaign_id == "campaign-1")
                .with_for_update()
            )
            assert room is not None
            _emit(
                callback_session,
                room,
                "room.activity",
                {
                    "schema": "sagasmith.room-activity/v1",
                    "run_id": context["run_id"],
                    "activity_id": "live-callback",
                    "audience": "public",
                    "audience_user_ids": [],
                    "code": "preparing_narration",
                    "state": "completed",
                },
            )
            callback_session.commit()
        return {
            "schema": "sagasmith.room-turn/v1",
            "run_id": context["run_id"],
            "messages": [
                {
                    "output_id": "invalid-ephemeral",
                    "audience": {"kind": "public", "actor_refs": []},
                    "blocks": [
                        {
                            "type": "performance",
                            "block_id": "invalid-speaker",
                            "speaker": {
                                "kind": "ephemeral",
                                "label": "Visitor",
                                "presentation_key": None,
                            },
                            "beats": [{"type": "speech", "text": "Hello"}],
                            "provenance": {"kind": "agent_ruling"},
                        }
                    ],
                }
            ],
            "suggestions": [],
        }

    agent_runtime.structured_output_factory = invalid_output
    response = client.post(
        "/api/campaigns/campaign-1/room/messages",
        headers={"Idempotency-Key": "live-activity-action"},
        json={"content": "I enter the room.", "mode": "action"},
    )
    assert response.status_code == 502, response.text

    with client.app.state.session_factory() as session:
        events = session.scalars(
            select(CampaignRoomEvent).order_by(CampaignRoomEvent.sequence)
        ).all()
    sequences = [event.sequence for event in events]
    assert len(sequences) == len(set(sequences))
    assert "room.activity" in [event.event_type for event in events]
    assert "agent.failed" in [event.event_type for event in events]
