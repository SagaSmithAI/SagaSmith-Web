from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from sagasmith_service.room_turn import RoomAudience

ROOM_ACTIVITY_SCHEMA = "sagasmith.room-activity/v1"
ActivityCode = Literal[
    "reviewing_rules",
    "checking_range",
    "resolving_roll",
    "settling_save",
    "awaiting_choice",
    "updating_state",
    "preparing_narration",
]
ActivityState = Literal[
    "started",
    "completed",
    "failed",
    "cancelled",
    "superseded",
]


class RoomActivitySubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: Literal[ROOM_ACTIVITY_SCHEMA] = Field(alias="schema")
    run_id: str = Field(min_length=1, max_length=64)
    activity_id: str = Field(min_length=1, max_length=80)
    audience: RoomAudience
    code: ActivityCode
    state: ActivityState


def room_activity_contract() -> dict[str, Any]:
    return {
        "name": "report_room_activity",
        "description": (
            "Report a short, finite-code, audience-safe host activity transition while the "
            "turn is running. Never include prose, reasoning, tool arguments, hidden facts, "
            "or indicate a hidden roll to players. Mechanical activity must use dm or actors "
            "audience, never public. Reuse activity_id when completing or cancelling it."
        ),
        "parameters": RoomActivitySubmission.model_json_schema(by_alias=True),
    }
