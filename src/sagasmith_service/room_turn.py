"""Versioned structured presentation contract for hosted campaign rooms."""

from __future__ import annotations

import hashlib
import hmac
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOM_TURN_SCHEMA = "sagasmith.room-turn/v1"


class RoomTurnModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class RoomAudience(RoomTurnModel):
    kind: Literal["public", "dm", "actors"] = "public"
    actor_refs: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def validate_actor_scope(self) -> RoomAudience:
        if self.kind == "actors" and not self.actor_refs:
            raise ValueError("actors audience requires actor_refs")
        if self.kind != "actors" and self.actor_refs:
            raise ValueError("actor_refs are only valid for actors audience")
        if any(not value.strip() or len(value) > 64 for value in self.actor_refs):
            raise ValueError("invalid audience actor reference")
        if len(set(self.actor_refs)) != len(self.actor_refs):
            raise ValueError("audience actor references must be unique")
        return self


class ActorPresentation(RoomTurnModel):
    kind: Literal["published_actor", "ephemeral"] = Field(
        description=(
            "Use published_actor for a human-controlled character. Ephemeral is only "
            "for a one-scene NPC or figure and cannot carry player_intent provenance."
        )
    )
    label: str = Field(min_length=1, max_length=160)
    actor_ref: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="Required only when kind is published_actor; otherwise null.",
    )
    presentation_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        description=(
            "Required and non-null when kind is ephemeral; use a stable, non-secret "
            "presentation key such as scene-innkeeper. Otherwise null."
        ),
    )
    portrait_ref: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_reference(self) -> ActorPresentation:
        if self.kind == "published_actor":
            if not self.actor_ref or self.presentation_key or self.portrait_ref:
                raise ValueError("published_actor requires only actor_ref")
        elif not self.presentation_key or self.actor_ref:
            raise ValueError("ephemeral speaker requires only presentation_key")
        elif self.portrait_ref and not self.portrait_ref.startswith("public-asset:"):
            raise ValueError("ephemeral portrait must use a Service-managed public asset")
        return self


class PerformanceBeat(RoomTurnModel):
    type: Literal["action", "speech"]
    text: str = Field(min_length=1, max_length=4000)


class PresentationProvenance(RoomTurnModel):
    kind: Literal["player_intent", "mcp_resolution", "agent_ruling"] = Field(
        description=(
            "player_intent requires a published_actor controlled by the triggering user; "
            "never use it with an ephemeral speaker."
        )
    )
    source_message_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="For player_intent, this must equal the triggering message id.",
    )
    resolution_id: str | None = Field(default=None, min_length=1, max_length=160)

    @model_validator(mode="after")
    def validate_source(self) -> PresentationProvenance:
        if self.kind == "player_intent":
            if not self.source_message_id or self.resolution_id:
                raise ValueError("player_intent requires only source_message_id")
        elif self.kind == "mcp_resolution":
            if not self.resolution_id or self.source_message_id:
                raise ValueError("mcp_resolution requires only resolution_id")
        elif self.source_message_id or self.resolution_id:
            raise ValueError("agent_ruling cannot claim an external source")
        return self


class NarrationBlock(RoomTurnModel):
    type: Literal["narration"]
    block_id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=8000)


class PerformanceBlock(RoomTurnModel):
    type: Literal["performance"]
    block_id: str = Field(min_length=1, max_length=80)
    speaker: ActorPresentation
    beats: list[PerformanceBeat] = Field(min_length=1, max_length=12)
    provenance: PresentationProvenance


class ResolutionReferenceBlock(RoomTurnModel):
    type: Literal["resolution_ref"]
    block_id: str = Field(min_length=1, max_length=80)
    resolution_id: str = Field(min_length=1, max_length=160)


class PromptBlock(RoomTurnModel):
    type: Literal["prompt"]
    block_id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=2000)


PresentationBlock = Annotated[
    NarrationBlock | PerformanceBlock | ResolutionReferenceBlock | PromptBlock,
    Field(discriminator="type"),
]


class RoomTurnMessage(RoomTurnModel):
    output_id: str = Field(min_length=1, max_length=80)
    audience: RoomAudience
    blocks: list[PresentationBlock] = Field(min_length=1, max_length=24)

    @model_validator(mode="after")
    def validate_block_ids(self) -> RoomTurnMessage:
        ids = [block.block_id for block in self.blocks]
        if len(ids) != len(set(ids)):
            raise ValueError("block_id must be unique inside a message")
        return self


class RoomSuggestion(RoomTurnModel):
    id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=500)
    actor_ref: str | None = Field(default=None, min_length=1, max_length=64)
    actor_revision: int | None = Field(default=None, ge=0)
    pending_choice_id: str | None = Field(default=None, min_length=1, max_length=160)


class RoomTurnSubmission(RoomTurnModel):
    schema_version: Literal[ROOM_TURN_SCHEMA] = Field(alias="schema")
    run_id: str = Field(min_length=1, max_length=64)
    messages: list[RoomTurnMessage] = Field(min_length=1, max_length=4)
    suggestions: list[RoomSuggestion] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def validate_ids(self) -> RoomTurnSubmission:
        output_ids = [message.output_id for message in self.messages]
        suggestion_ids = [suggestion.id for suggestion in self.suggestions]
        if len(output_ids) != len(set(output_ids)):
            raise ValueError("output_id must be unique")
        if len(suggestion_ids) != len(set(suggestion_ids)):
            raise ValueError("suggestion id must be unique")
        return self


def room_turn_contract() -> dict[str, Any]:
    """Return the one authoritative model-facing terminal tool contract."""

    return {
        "name": "submit_room_turn",
        "description": (
            "Submit the final audience-safe SagaSmith room presentation. Call exactly once "
            "after authoritative mechanics are complete. Never include chain-of-thought, "
            "system prompts, tool parameters, hidden facts, HTML, or markdown role markers."
        ),
        "parameters": RoomTurnSubmission.model_json_schema(by_alias=True),
    }


def publication_ref(secret: Any, campaign_id: str, actor_ref: str) -> str:
    """Create a stable browser-safe actor publication reference."""

    reveal = getattr(secret, "get_secret_value", None)
    secret_value = reveal() if callable(reveal) else str(secret)
    digest = hmac.new(
        secret_value.encode("utf-8"),
        f"{campaign_id}:{actor_ref}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:24]
    return f"actor-pub-{digest}"


def project_speaker(
    speaker: ActorPresentation,
    *,
    secret: Any,
    campaign_id: str,
) -> dict[str, Any]:
    """Strip private actor ids before a presentation reaches room storage."""

    value: dict[str, Any] = {
        "kind": speaker.kind,
        "label": speaker.label,
    }
    if speaker.portrait_ref:
        value["portrait_ref"] = speaker.portrait_ref
    if speaker.kind == "published_actor":
        assert speaker.actor_ref is not None
        value["publication_ref"] = publication_ref(secret, campaign_id, speaker.actor_ref)
    else:
        value["presentation_key"] = speaker.presentation_key
    return value


def plain_text_for_blocks(blocks: list[dict[str, Any]]) -> str:
    """Mechanically flatten already-safe blocks for transcripts and accessibility."""

    lines: list[str] = []
    for block in blocks:
        block_type = block.get("type")
        if block_type in {"narration", "prompt"}:
            lines.append(str(block.get("text") or ""))
        elif block_type == "performance":
            speaker = block.get("speaker") or {}
            label = str(speaker.get("label") or "角色")
            for beat in block.get("beats") or []:
                text = str(beat.get("text") or "")
                if beat.get("type") == "speech":
                    lines.append(f"{label}：“{text}”")
                else:
                    lines.append(f"{label}：{text}")
        elif block_type == "resolution_ref":
            lines.append("（判定结果见结算气泡）")
    return "\n".join(line for line in lines if line).strip()
