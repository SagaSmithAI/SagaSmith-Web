from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sagasmith_service.database import Base


def new_id() -> str:
    return str(uuid.uuid4())


def now_utc() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    sessions: Mapped[list[UserSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def principal_id(self) -> str:
        return f"user:{self.id}"


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)

    user: Mapped[User] = relationship(back_populates="sessions")


class Plan(TimestampMixin, Base):
    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(50), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    limits: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Subscription(TimestampMixin, Base):
    __tablename__ = "subscriptions"
    __table_args__ = (UniqueConstraint("user_id", "status", name="uq_active_user_subscription"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"))
    status: Mapped[str] = mapped_column(String(24), default="active")
    current_period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    current_period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class QuotaGrant(Base):
    __tablename__ = "quota_grants"
    __table_args__ = (
        Index("ix_quota_grant_lookup", "user_id", "metric", "period_start", "period_end"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    metric: Mapped[str] = mapped_column(String(50))
    quantity: Mapped[Decimal] = mapped_column(Numeric(24, 6))
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(50), default="plan")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class QuotaReservation(Base):
    __tablename__ = "quota_reservations"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_quota_reservation_retry"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    campaign_id: Mapped[str | None] = mapped_column(String(64), index=True)
    metric: Mapped[str] = mapped_column(String(50), index=True)
    reserved_quantity: Mapped[Decimal] = mapped_column(Numeric(24, 6))
    settled_quantity: Mapped[Decimal] = mapped_column(Numeric(24, 6), default=Decimal("0"))
    status: Mapped[str] = mapped_column(String(24), default="reserved", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UsageLedger(Base):
    __tablename__ = "usage_ledger"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_usage_retry"),
        Index("ix_usage_period", "user_id", "metric", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    campaign_id: Mapped[str | None] = mapped_column(String(64), index=True)
    reservation_id: Mapped[str | None] = mapped_column(
        ForeignKey("quota_reservations.id", ondelete="SET NULL")
    )
    metric: Mapped[str] = mapped_column(String(50))
    quantity: Mapped[Decimal] = mapped_column(Numeric(24, 6))
    unit: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str | None] = mapped_column(String(80))
    model: Mapped[str | None] = mapped_column(String(120))
    request_id: Mapped[str | None] = mapped_column(String(100), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class CampaignProjection(TimestampMixin, Base):
    __tablename__ = "campaign_projections"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    visibility: Mapped[str] = mapped_column(String(24), default="private", index=True)
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    system_id: Mapped[str] = mapped_column(String(32), default="dnd5e")
    purpose: Mapped[str] = mapped_column(String(24), default="play", index=True)
    mcp_revision: Mapped[int] = mapped_column(default=1)
    mcp_receipt: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class CampaignMembershipProjection(TimestampMixin, Base):
    __tablename__ = "campaign_membership_projections"
    __table_args__ = (UniqueConstraint("campaign_id", "user_id", name="uq_campaign_member"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaign_projections.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(24), index=True)
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    authorization_epoch: Mapped[int] = mapped_column(default=1)
    mcp_receipt: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class CampaignPanelProjection(TimestampMixin, Base):
    """Versioned, principal-scoped read model published by the domain MCP."""

    __tablename__ = "campaign_panel_projections"
    __table_args__ = (
        UniqueConstraint(
            "campaign_id",
            "audience_key",
            name="uq_campaign_panel_projection_audience",
        ),
        Index(
            "ix_campaign_panel_projection_freshness",
            "campaign_id",
            "audience_key",
            "source_revision",
            "authorization_epoch",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaign_projections.id", ondelete="CASCADE"), index=True
    )
    audience_key: Mapped[str] = mapped_column(String(100))
    source_revision: Mapped[int] = mapped_column(default=0)
    authorization_epoch: Mapped[int] = mapped_column(default=0)
    projection_schema_version: Mapped[int] = mapped_column(default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class JoinRequest(TimestampMixin, Base):
    __tablename__ = "join_requests"
    __table_args__ = (
        Index("ix_join_request_pending", "campaign_id", "applicant_user_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaign_projections.id", ondelete="CASCADE"), index=True
    )
    applicant_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    requested_role: Mapped[str] = mapped_column(String(24), default="player")
    message: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    reviewed_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mcp_receipt: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class CampaignInvite(TimestampMixin, Base):
    __tablename__ = "campaign_invites"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaign_projections.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    intended_role: Mapped[str] = mapped_column(String(24), default="player")
    mode: Mapped[str] = mapped_column(String(24), default="request")
    created_by_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    max_uses: Mapped[int] = mapped_column(default=1)
    used_count: Mapped[int] = mapped_column(default=0)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ActorBindingProjection(TimestampMixin, Base):
    __tablename__ = "actor_binding_projections"
    __table_args__ = (
        UniqueConstraint("campaign_id", "actor_id", "user_id", name="uq_actor_user_binding"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaign_projections.id", ondelete="CASCADE"), index=True
    )
    actor_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    can_control: Mapped[bool] = mapped_column(Boolean, default=True)
    can_view_private: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(24), default="active")
    mcp_receipt: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AgentConversation(TimestampMixin, Base):
    __tablename__ = "agent_conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaign_projections.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    identity_assignment_id: Mapped[str | None] = mapped_column(
        ForeignKey("identity_campaign_assignments.id", ondelete="SET NULL"), index=True
    )
    title: Mapped[str] = mapped_column(String(160), default="新会话")
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)


class CampaignRoom(TimestampMixin, Base):
    __tablename__ = "campaign_rooms"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaign_projections.id", ondelete="CASCADE"), unique=True, index=True
    )
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    host_identity_assignment_id: Mapped[str | None] = mapped_column(
        ForeignKey("identity_campaign_assignments.id", ondelete="SET NULL"), index=True
    )
    next_message_sequence: Mapped[int] = mapped_column(default=1)
    next_event_sequence: Mapped[int] = mapped_column(default=1)


class CampaignMessage(Base):
    __tablename__ = "campaign_messages"
    __table_args__ = (
        UniqueConstraint("room_id", "sequence", name="uq_campaign_message_sequence"),
        UniqueConstraint("room_id", "client_message_id", name="uq_campaign_message_retry"),
        Index("ix_campaign_message_timeline", "room_id", "sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    room_id: Mapped[str] = mapped_column(
        ForeignKey("campaign_rooms.id", ondelete="CASCADE"), index=True
    )
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaign_projections.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column()
    sender_type: Mapped[str] = mapped_column(String(24), index=True)
    sender_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    sender_display_name: Mapped[str] = mapped_column(String(160))
    message_type: Mapped[str] = mapped_column(String(32), default="chat", index=True)
    audience: Mapped[str] = mapped_column(String(24), default="public", index=True)
    audience_user_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    content: Mapped[str] = mapped_column(Text)
    structured_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    reply_to_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("campaign_messages.id", ondelete="SET NULL"), index=True
    )
    trigger_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("campaign_messages.id", ondelete="SET NULL"), index=True
    )
    mcp_revision: Mapped[int | None] = mapped_column()
    mcp_receipt: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="completed", index=True)
    client_message_id: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CampaignSuggestion(Base):
    __tablename__ = "campaign_suggestions"
    __table_args__ = (
        UniqueConstraint(
            "message_id",
            "suggestion_id",
            name="uq_campaign_suggestion_message_id",
        ),
        Index(
            "ix_campaign_suggestion_active_target",
            "room_id",
            "target_user_id",
            "expired",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    room_id: Mapped[str] = mapped_column(
        ForeignKey("campaign_rooms.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[str] = mapped_column(
        ForeignKey("campaign_messages.id", ondelete="CASCADE"), index=True
    )
    suggestion_id: Mapped[str] = mapped_column(String(80))
    target_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    actor_ref: Mapped[str | None] = mapped_column(String(64))
    run_id: Mapped[str] = mapped_column(String(64))
    expired: Mapped[bool] = mapped_column(Boolean, default=False)
    valid_revision: Mapped[int | None] = mapped_column()
    valid_phase: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class CampaignRoomEvent(Base):
    __tablename__ = "campaign_room_events"
    __table_args__ = (
        UniqueConstraint("room_id", "sequence", name="uq_campaign_room_event_sequence"),
        Index("ix_campaign_room_event_stream", "room_id", "sequence"),
        Index(
            "ix_campaign_room_event_activity",
            "room_id",
            "run_id",
            "activity_id",
            "sequence",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    room_id: Mapped[str] = mapped_column(
        ForeignKey("campaign_rooms.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column()
    event_type: Mapped[str] = mapped_column(String(50), index=True)
    run_id: Mapped[str | None] = mapped_column(String(64))
    activity_id: Mapped[str | None] = mapped_column(String(80))
    activity_state: Mapped[str | None] = mapped_column(String(24))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class CampaignRoomReadCursor(Base):
    __tablename__ = "campaign_room_read_cursors"
    __table_args__ = (UniqueConstraint("room_id", "user_id", name="uq_campaign_room_reader"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    room_id: Mapped[str] = mapped_column(
        ForeignKey("campaign_rooms.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    last_read_sequence: Mapped[int] = mapped_column(default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc
    )


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_agent_run_retry"),
        Index("ix_agent_run_conversation", "conversation_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("agent_conversations.id", ondelete="CASCADE"), index=True
    )
    trigger_message_id: Mapped[str | None] = mapped_column(
        ForeignKey("campaign_messages.id", ondelete="SET NULL"), index=True
    )
    campaign_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    request_hash: Mapped[str] = mapped_column(String(64))
    user_content: Mapped[str] = mapped_column(Text)
    assistant_content: Mapped[str | None] = mapped_column(Text)
    upstream_request_id: Mapped[str | None] = mapped_column(String(100))
    model: Mapped[str | None] = mapped_column(String(120))
    prompt_tokens: Mapped[int] = mapped_column(default=0)
    completion_tokens: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(24), default="running", index=True)
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PrivatePack(TimestampMixin, Base):
    __tablename__ = "private_packs"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "pack_id", "version", name="uq_private_pack_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    pack_id: Mapped[str] = mapped_column(String(160), index=True)
    version: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(24), index=True)
    storage_key: Mapped[str] = mapped_column(String(500), unique=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int] = mapped_column()
    media_type: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(24), default="private", index=True)
    rights_attested: Mapped[bool] = mapped_column(Boolean, default=False)
    distribution: Mapped[str] = mapped_column(String(24), default="private")
    review_notes: Mapped[str] = mapped_column(String(2000), default="")


class CampaignPackProjection(TimestampMixin, Base):
    __tablename__ = "campaign_pack_projections"
    __table_args__ = (
        UniqueConstraint("campaign_id", "private_pack_id", name="uq_campaign_private_pack"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaign_projections.id", ondelete="CASCADE"), index=True
    )
    private_pack_id: Mapped[str] = mapped_column(
        ForeignKey("private_packs.id", ondelete="RESTRICT"), index=True
    )
    imported_by_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(24), default="imported", index=True)
    runtime_ref: Mapped[str | None] = mapped_column(String(160))
    mcp_receipt: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Artifact(TimestampMixin, Base):
    """A shareable work. Campaign runtime state never belongs in this table."""

    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "slug", name="uq_artifact_owner_slug"),
        Index("ix_artifact_catalog", "visibility", "status", "artifact_type"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    slug: Mapped[str] = mapped_column(String(100))
    artifact_type: Mapped[str] = mapped_column(String(24), index=True)
    title: Mapped[str] = mapped_column(String(200), index=True)
    summary: Mapped[str] = mapped_column(String(2000), default="")
    system_id: Mapped[str] = mapped_column(String(32), default="dnd5e", index=True)
    visibility: Mapped[str] = mapped_column(String(24), default="private", index=True)
    status: Mapped[str] = mapped_column(String(24), default="draft", index=True)
    license_code: Mapped[str] = mapped_column(String(64), default="ARR")
    rights_attested: Mapped[bool] = mapped_column(Boolean, default=False)
    source_kind: Mapped[str] = mapped_column(String(32), default="original")
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    forked_from_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL"), index=True
    )
    discussion_enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class ArtifactCollaborator(TimestampMixin, Base):
    __tablename__ = "artifact_collaborators"
    __table_args__ = (UniqueConstraint("artifact_id", "user_id", name="uq_artifact_collaborator"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(24), default="editor")
    status: Mapped[str] = mapped_column(String(24), default="active")


class ArtifactRelease(TimestampMixin, Base):
    """An immutable publication unit after it reaches ``published``."""

    __tablename__ = "artifact_releases"
    __table_args__ = (
        UniqueConstraint("artifact_id", "version", name="uq_artifact_release_version"),
        Index("ix_artifact_release_catalog", "status", "published_at"),
        Index(
            "ix_artifact_release_latest",
            "artifact_id",
            "status",
            "published_at",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    changelog: Mapped[str] = mapped_column(String(4000), default="")
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    compatibility: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    private_pack_id: Mapped[str | None] = mapped_column(
        ForeignKey("private_packs.id", ondelete="RESTRICT"), index=True
    )
    module_project_id: Mapped[str | None] = mapped_column(
        ForeignKey("module_projects.id", ondelete="RESTRICT"), index=True
    )
    content_artifact: Mapped[str | None] = mapped_column(String(500))
    content_checksum: Mapped[str | None] = mapped_column(String(64), index=True)
    contains_private_source: Mapped[bool] = mapped_column(Boolean, default=False)
    agent_review: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    agent_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    moderated_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    moderation_notes: Mapped[str] = mapped_column(String(2000), default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ArtifactFavorite(Base):
    __tablename__ = "artifact_favorites"
    __table_args__ = (UniqueConstraint("artifact_id", "user_id", name="uq_artifact_favorite"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class ArtifactInstallation(TimestampMixin, Base):
    __tablename__ = "artifact_installations"
    __table_args__ = (
        UniqueConstraint(
            "installed_by_user_id",
            "release_id",
            "target_key",
            name="uq_artifact_install_target",
        ),
        Index("ix_artifact_install_owner", "installed_by_user_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.id", ondelete="RESTRICT"), index=True
    )
    release_id: Mapped[str] = mapped_column(
        ForeignKey("artifact_releases.id", ondelete="RESTRICT"), index=True
    )
    installed_by_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    campaign_id: Mapped[str | None] = mapped_column(
        ForeignKey("campaign_projections.id", ondelete="CASCADE"), index=True
    )
    target_key: Mapped[str] = mapped_column(String(100))
    install_kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24), default="installed", index=True)
    runtime_ref: Mapped[str | None] = mapped_column(String(160))
    campaign_pack_projection_id: Mapped[str | None] = mapped_column(
        ForeignKey("campaign_pack_projections.id", ondelete="SET NULL")
    )
    receipt: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class CommunityPost(TimestampMixin, Base):
    __tablename__ = "community_posts"
    __table_args__ = (Index("ix_community_post_target", "target_type", "target_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    author_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    target_type: Mapped[str] = mapped_column(String(24))
    target_id: Mapped[str] = mapped_column(String(36))
    release_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifact_releases.id", ondelete="SET NULL"), index=True
    )
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("community_posts.id", ondelete="CASCADE"), index=True
    )
    category: Mapped[str] = mapped_column(String(24), default="discussion")
    audience: Mapped[str] = mapped_column(String(24), default="public")
    spoiler: Mapped[bool] = mapped_column(Boolean, default=False)
    body: Mapped[str] = mapped_column(String(10_000))
    status: Mapped[str] = mapped_column(String(24), default="visible", index=True)


class CommunityReport(TimestampMixin, Base):
    __tablename__ = "community_reports"
    __table_args__ = (Index("ix_community_report_queue", "status", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    reporter_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    target_type: Mapped[str] = mapped_column(String(24))
    target_id: Mapped[str] = mapped_column(String(36), index=True)
    reason: Mapped[str] = mapped_column(String(32))
    details: Mapped[str] = mapped_column(String(2000), default="")
    status: Mapped[str] = mapped_column(String(24), default="open", index=True)
    reviewed_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    resolution: Mapped[str] = mapped_column(String(2000), default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentIdentity(TimestampMixin, Base):
    """A persistent hosted persona. Private campaign memory is never stored here."""

    __tablename__ = "agent_identities"
    __table_args__ = (
        UniqueConstraint("handle", name="uq_agent_identity_handle"),
        Index("ix_agent_identity_catalog", "visibility", "status", "identity_kind"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    handle: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(160), index=True)
    identity_kind: Mapped[str] = mapped_column(String(24), index=True)
    system_id: Mapped[str] = mapped_column(String(32), index=True)
    bio: Mapped[str] = mapped_column(String(2000), default="")
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    visibility: Mapped[str] = mapped_column(String(24), default="private", index=True)
    status: Mapped[str] = mapped_column(String(24), default="draft", index=True)
    availability: Mapped[str] = mapped_column(String(24), default="unavailable")
    active_soul_release_id: Mapped[str] = mapped_column(
        ForeignKey("artifact_releases.id", ondelete="RESTRICT"), index=True
    )
    memory_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    public_profile: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    @property
    def principal_id(self) -> str:
        return f"agent:{self.id}"


class IdentityCampaignAssignment(TimestampMixin, Base):
    __tablename__ = "identity_campaign_assignments"
    __table_args__ = (
        UniqueConstraint("invited_by_user_id", "idempotency_key", name="uq_identity_invite_retry"),
        Index("ix_identity_assignment_active", "campaign_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    identity_id: Mapped[str] = mapped_column(
        ForeignKey("agent_identities.id", ondelete="CASCADE"), index=True
    )
    active_key: Mapped[str | None] = mapped_column(String(140), unique=True, index=True)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaign_projections.id", ondelete="CASCADE"), index=True
    )
    soul_release_id: Mapped[str] = mapped_column(
        ForeignKey("artifact_releases.id", ondelete="RESTRICT")
    )
    role: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    invited_by_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    quota_payer_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    idempotency_key: Mapped[str] = mapped_column(String(160))
    memory_namespace: Mapped[str] = mapped_column(String(200))
    mcp_receipt: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IdentityMemoryEntry(TimestampMixin, Base):
    __tablename__ = "identity_memory_entries"
    __table_args__ = (
        UniqueConstraint("assignment_id", "memory_key", name="uq_identity_memory_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    assignment_id: Mapped[str] = mapped_column(
        ForeignKey("identity_campaign_assignments.id", ondelete="CASCADE"), index=True
    )
    memory_key: Mapped[str] = mapped_column(String(100))
    content: Mapped[str] = mapped_column(Text)
    audience: Mapped[str] = mapped_column(String(24), default="dm")
    source: Mapped[str] = mapped_column(String(32), default="curated")
    revision: Mapped[int] = mapped_column(default=1)


class ModuleProject(TimestampMixin, Base):
    """Product projection of one MCP-owned D&D module authoring workflow."""

    __tablename__ = "module_projects"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "slug", name="uq_module_project_owner_slug"),
        Index("ix_module_project_owner_status", "owner_user_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    authoring_campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaign_projections.id", ondelete="RESTRICT"), unique=True, index=True
    )
    slug: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(200))
    brief: Mapped[str] = mapped_column(Text)
    system_id: Mapped[str] = mapped_column(String(32), default="dnd5e")
    edition: Mapped[str] = mapped_column(String(16), default="2024")
    locale: Mapped[str] = mapped_column(String(20), default="zh-CN")
    version: Mapped[str] = mapped_column(String(80), default="0.1.0")
    status: Mapped[str] = mapped_column(String(32), default="idea", index=True)
    specification: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    outline: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    outline_revision: Mapped[int] = mapped_column(default=0)
    current_source_id: Mapped[str | None] = mapped_column(String(36), index=True)
    mcp_job_id: Mapped[str | None] = mapped_column(String(64), index=True)
    mcp_module_id: Mapped[str | None] = mapped_column(String(160))
    mcp_draft_revision: Mapped[int | None] = mapped_column()
    mcp_draft_state: Mapped[str | None] = mapped_column(String(32))
    inspection: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    validation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    review: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    package_decisions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    final_artifact: Mapped[str | None] = mapped_column(String(500))
    final_pack_id: Mapped[str | None] = mapped_column(String(160), index=True)
    final_checksum: Mapped[str | None] = mapped_column(String(64), index=True)
    finalization: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    published_release_id: Mapped[str | None] = mapped_column(String(36), index=True)
    budget_tokens: Mapped[int] = mapped_column(default=500_000)
    used_tokens: Mapped[int] = mapped_column(default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error: Mapped[str] = mapped_column(String(2000), default="")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ModuleSource(TimestampMixin, Base):
    __tablename__ = "module_sources"
    __table_args__ = (
        UniqueConstraint("project_id", "generation", name="uq_module_source_generation"),
        Index("ix_module_source_project_status", "project_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("module_projects.id", ondelete="CASCADE"), index=True
    )
    generation: Mapped[int] = mapped_column()
    source_type: Mapped[str] = mapped_column(String(24))
    name: Mapped[str] = mapped_column(String(255))
    storage_key: Mapped[str] = mapped_column(String(500), unique=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int] = mapped_column()
    media_type: Mapped[str] = mapped_column(String(120))
    rights_basis: Mapped[str] = mapped_column(String(32))
    license_code: Mapped[str] = mapped_column(String(64), default="ARR")
    attribution: Mapped[str] = mapped_column(String(2000), default="")
    public_eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(24), default="ready", index=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ModuleRun(Base):
    __tablename__ = "module_runs"
    __table_args__ = (
        UniqueConstraint("requested_by_user_id", "idempotency_key", name="uq_module_run_retry"),
        Index("ix_module_run_queue", "status", "available_at", "created_at"),
        Index("ix_module_run_project", "project_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("module_projects.id", ondelete="CASCADE"), index=True
    )
    requested_by_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    run_type: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    input_hash: Mapped[str] = mapped_column(String(64))
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    attempt: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=3)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    lease_owner: Mapped[str | None] = mapped_column(String(100), index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    reservation_id: Mapped[str | None] = mapped_column(
        ForeignKey("quota_reservations.id", ondelete="SET NULL")
    )
    prompt_tokens: Mapped[int] = mapped_column(default=0)
    completion_tokens: Mapped[int] = mapped_column(default=0)
    model: Mapped[str | None] = mapped_column(String(120))
    upstream_request_id: Mapped[str | None] = mapped_column(String(100))
    error: Mapped[str] = mapped_column(String(2000), default="")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ModuleDecision(Base):
    __tablename__ = "module_decisions"
    __table_args__ = (Index("ix_module_decision_project", "project_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("module_projects.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("module_runs.id", ondelete="SET NULL"), index=True
    )
    actor_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    decision_type: Mapped[str] = mapped_column(String(40), index=True)
    project_revision: Mapped[int] = mapped_column()
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class ModuleInstallation(TimestampMixin, Base):
    __tablename__ = "module_installations"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "version", "campaign_id", name="uq_module_project_install_target"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("module_projects.id", ondelete="RESTRICT"), index=True
    )
    version: Mapped[str] = mapped_column(String(80))
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaign_projections.id", ondelete="CASCADE"), index=True
    )
    installed_by_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String(24), default="installed", index=True)
    runtime_module_id: Mapped[str | None] = mapped_column(String(160))
    receipt: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class UserNotification(Base):
    __tablename__ = "user_notifications"
    __table_args__ = (Index("ix_user_notification_inbox", "user_id", "read_at", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    notification_type: Mapped[str] = mapped_column(String(50), index=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(String(2000), default="")
    action_url: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (Index("ix_outbox_pending", "status", "available_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    aggregate_type: Mapped[str] = mapped_column(String(50))
    aggregate_id: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(1000))


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_subject", "subject_type", "subject_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(100), index=True)
    subject_type: Mapped[str] = mapped_column(String(50))
    subject_id: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(100), index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
