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
    mcp_revision: Mapped[int] = mapped_column(default=1)
    mcp_receipt: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class CampaignMembershipProjection(TimestampMixin, Base):
    __tablename__ = "campaign_membership_projections"
    __table_args__ = (
        UniqueConstraint("campaign_id", "user_id", name="uq_campaign_member"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("campaign_projections.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(24), index=True)
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    mcp_receipt: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


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
    title: Mapped[str] = mapped_column(String(160), default="新会话")
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)


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
