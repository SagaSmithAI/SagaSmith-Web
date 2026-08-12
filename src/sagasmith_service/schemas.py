from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RegisterRequest(ApiModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=200)
    display_name: str = Field(min_length=1, max_length=80)


class LoginRequest(ApiModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class UserView(ApiModel):
    id: str
    email: EmailStr
    display_name: str
    status: str
    is_admin: bool
    principal_id: str


class AuthResult(ApiModel):
    user: UserView


class CampaignCreateRequest(ApiModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    edition: Literal["2014", "2024"] = "2024"
    locale: str = Field(default="zh-CN", min_length=2, max_length=20)
    advancement_mode: Literal["milestone", "xp"] = "milestone"
    visibility: Literal["private", "unlisted"] = "private"


class CampaignView(ApiModel):
    id: str
    name: str
    owner_user_id: str
    visibility: str
    status: str
    system_id: str
    mcp_revision: int


class MembershipView(ApiModel):
    user_id: str
    display_name: str
    role: str
    status: str


class MembershipRoleUpdate(ApiModel):
    role: Literal["dm", "player"]


class JoinRequestCreate(ApiModel):
    message: str = Field(default="", max_length=500)


class JoinDecisionRequest(ApiModel):
    decision: Literal["approved", "rejected"]


class JoinRequestView(ApiModel):
    id: str
    campaign_id: str
    applicant_user_id: str
    requested_role: str
    message: str
    status: str


class ActorBindingRequest(ApiModel):
    user_id: str
    can_control: bool = True
    can_view_private: bool = True


class ActorBindingView(ApiModel):
    campaign_id: str
    actor_id: str
    user_id: str
    can_control: bool
    can_view_private: bool
    status: str


class QuotaBalanceView(ApiModel):
    metric: str
    granted: Decimal
    used: Decimal
    reserved: Decimal
    available: Decimal


class ConversationCreate(ApiModel):
    title: str = Field(default="新会话", min_length=1, max_length=160)
    identity_assignment_id: str | None = None


class ConversationView(ApiModel):
    id: str
    campaign_id: str
    user_id: str
    title: str
    status: str
    identity_assignment_id: str | None


class AgentMessageRequest(ApiModel):
    content: str = Field(min_length=1, max_length=100_000)


class AgentRunView(ApiModel):
    id: str
    conversation_id: str
    campaign_id: str
    user_id: str
    user_content: str
    assistant_content: str | None
    model: str | None
    prompt_tokens: int
    completion_tokens: int
    status: str


class UsageLedgerView(ApiModel):
    id: str
    campaign_id: str | None
    metric: str
    quantity: Decimal
    unit: str
    provider: str | None
    model: str | None
    request_id: str | None
    occurred_at: datetime


class PrivatePackView(ApiModel):
    id: str
    pack_id: str
    version: str
    title: str
    kind: str
    sha256: str
    size_bytes: int
    media_type: str
    status: str
    distribution: str


class CampaignPackView(ApiModel):
    id: str
    campaign_id: str
    private_pack_id: str
    status: str
    runtime_ref: str | None


class InviteCreate(ApiModel):
    mode: Literal["request", "auto_join"] = "request"
    expires_in_hours: int = Field(default=72, ge=1, le=24 * 30)
    max_uses: int = Field(default=1, ge=1, le=100)


class InviteView(ApiModel):
    id: str
    campaign_id: str
    intended_role: str
    mode: str
    expires_at: datetime
    max_uses: int
    used_count: int


class InviteCreated(ApiModel):
    invite: InviteView
    token: str


class InviteAcceptRequest(ApiModel):
    token: str = Field(min_length=32, max_length=200)
    message: str = Field(default="", max_length=500)


class AdminQuotaGrantRequest(ApiModel):
    metric: Literal["llm_tokens"] = "llm_tokens"
    quantity: int = Field(gt=0, le=1_000_000_000)
    valid_days: int = Field(default=30, ge=1, le=366)


class QuotaGrantView(ApiModel):
    id: str
    user_id: str
    metric: str
    quantity: Decimal
    source: str


class AuditEventView(ApiModel):
    id: str
    actor_user_id: str | None
    action: str
    subject_type: str
    subject_id: str
    request_id: str | None
    details: dict
    created_at: datetime


ArtifactType = Literal["module", "rule", "character", "soul", "skill", "asset"]
Tag = Annotated[str, Field(min_length=1, max_length=40, pattern=r"^[^<>]+$")]


def _json_bytes(*values: Any) -> int:
    return len(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class ArtifactCreate(ApiModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,99}$")
    artifact_type: ArtifactType
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(default="", max_length=2000)
    system_id: Literal["dnd5e", "coc7e", "system-neutral"] = "dnd5e"
    visibility: Literal["private", "unlisted", "public"] = "private"
    license_code: str = Field(default="ARR", min_length=2, max_length=64)
    rights_attested: bool = False
    source_kind: Literal["original", "open_licensed", "private_source"] = "original"
    provenance: dict[str, Any] = Field(default_factory=dict)
    tags: list[Tag] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def limit_metadata(self) -> ArtifactCreate:
        if _json_bytes(self.provenance, self.tags) > 32_000:
            raise ValueError("artifact metadata is too large")
        return self


class ArtifactUpdate(ApiModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    summary: str | None = Field(default=None, max_length=2000)
    visibility: Literal["private", "unlisted", "public"] | None = None
    license_code: str | None = Field(default=None, min_length=2, max_length=64)
    rights_attested: bool | None = None
    source_kind: Literal["original", "open_licensed", "private_source"] | None = None
    provenance: dict[str, Any] | None = None
    tags: list[Tag] | None = Field(default=None, max_length=20)
    discussion_enabled: bool | None = None

    @model_validator(mode="after")
    def limit_metadata(self) -> ArtifactUpdate:
        if _json_bytes(self.provenance or {}, self.tags or []) > 32_000:
            raise ValueError("artifact metadata is too large")
        return self


class ArtifactView(ApiModel):
    id: str
    owner_user_id: str
    owner_display_name: str
    slug: str
    artifact_type: str
    title: str
    summary: str
    system_id: str
    visibility: str
    status: str
    license_code: str
    rights_attested: bool
    source_kind: str
    provenance: dict[str, Any]
    tags: list[str]
    forked_from_artifact_id: str | None
    discussion_enabled: bool
    favorite_count: int = 0
    latest_release_id: str | None = None
    latest_version: str | None = None


class ArtifactReleaseCreate(ApiModel):
    version: str = Field(min_length=1, max_length=80)
    changelog: str = Field(default="", max_length=4000)
    manifest: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
    compatibility: dict[str, Any] = Field(default_factory=dict)
    private_pack_id: str | None = None
    contains_private_source: bool = False

    @model_validator(mode="after")
    def limit_release_json(self) -> ArtifactReleaseCreate:
        if _json_bytes(self.manifest, self.payload, self.compatibility) > 256_000:
            raise ValueError("release JSON payload is too large; use a Pack archive")
        return self


class ArtifactForkCreate(ApiModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,99}$")
    title: str = Field(min_length=1, max_length=200)


class ArtifactReleaseView(ApiModel):
    id: str
    artifact_id: str
    version: str
    status: str
    changelog: str
    manifest: dict[str, Any]
    payload: dict[str, Any]
    compatibility: dict[str, Any]
    private_pack_id: str | None
    contains_private_source: bool
    agent_review: dict[str, Any]
    moderation_notes: str
    published_at: datetime | None


class AgentReviewRecord(ApiModel):
    approved: bool
    summary: str = Field(min_length=1, max_length=2000)
    findings: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    reviewer: str = Field(default="hosted-agent", min_length=1, max_length=160)


class ModerationDecision(ApiModel):
    decision: Literal["approved", "rejected", "withdrawn"]
    notes: str = Field(default="", max_length=2000)


class ArtifactInstallRequest(ApiModel):
    campaign_id: str | None = None
    activate: bool = False


class ArtifactInstallationView(ApiModel):
    id: str
    artifact_id: str
    release_id: str
    installed_by_user_id: str
    campaign_id: str | None
    install_kind: str
    status: str
    runtime_ref: str | None


class CollaboratorCreate(ApiModel):
    user_id: str
    role: Literal["editor", "reviewer"] = "editor"


class CommunityPostCreate(ApiModel):
    target_type: Literal["artifact", "identity"]
    target_id: str
    release_id: str | None = None
    parent_id: str | None = None
    category: Literal[
        "discussion", "errata", "rules", "play_report", "suggestion", "announcement"
    ] = "discussion"
    audience: Literal["public", "owners"] = "public"
    spoiler: bool = False
    body: str = Field(min_length=1, max_length=10_000)


class CommunityPostView(ApiModel):
    id: str
    author_user_id: str
    author_display_name: str
    target_type: str
    target_id: str
    release_id: str | None
    parent_id: str | None
    category: str
    audience: str
    spoiler: bool
    body: str
    status: str
    created_at: datetime


class CommunityReportCreate(ApiModel):
    target_type: Literal["artifact", "identity", "post"]
    target_id: str
    reason: Literal["copyright", "commercial_source", "privacy", "abuse", "malware", "other"]
    details: str = Field(default="", max_length=2000)


class CommunityReportDecision(ApiModel):
    status: Literal["resolved", "dismissed"]
    resolution: str = Field(min_length=1, max_length=2000)


class CommunityReportView(ApiModel):
    id: str
    reporter_user_id: str
    target_type: str
    target_id: str
    reason: str
    details: str
    status: str
    resolution: str
    created_at: datetime


class AgentIdentityCreate(ApiModel):
    handle: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,79}$")
    name: str = Field(min_length=1, max_length=160)
    identity_kind: Literal["dm", "keeper", "npc"]
    system_id: Literal["dnd5e", "coc7e"]
    bio: str = Field(default="", max_length=2000)
    avatar_url: str | None = Field(default=None, max_length=500)
    visibility: Literal["private", "unlisted", "public"] = "private"
    availability: Literal["unavailable", "invite_only", "available"] = "unavailable"
    active_soul_release_id: str
    memory_policy: dict[str, Any] = Field(default_factory=dict)
    public_profile: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def limit_identity_metadata(self) -> AgentIdentityCreate:
        if _json_bytes(self.memory_policy, self.public_profile) > 32_000:
            raise ValueError("Identity metadata is too large")
        return self


class AgentIdentityUpdate(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    bio: str | None = Field(default=None, max_length=2000)
    avatar_url: str | None = Field(default=None, max_length=500)
    visibility: Literal["private", "unlisted", "public"] | None = None
    availability: Literal["unavailable", "invite_only", "available"] | None = None
    active_soul_release_id: str | None = None
    memory_policy: dict[str, Any] | None = None
    public_profile: dict[str, Any] | None = None

    @model_validator(mode="after")
    def limit_identity_metadata(self) -> AgentIdentityUpdate:
        if _json_bytes(self.memory_policy or {}, self.public_profile or {}) > 32_000:
            raise ValueError("Identity metadata is too large")
        return self


class AgentIdentityView(ApiModel):
    id: str
    owner_user_id: str
    owner_display_name: str
    handle: str
    name: str
    identity_kind: str
    system_id: str
    bio: str
    avatar_url: str | None
    visibility: str
    status: str
    availability: str
    active_soul_release_id: str
    memory_policy: dict[str, Any]
    public_profile: dict[str, Any]
    principal_id: str


class IdentityInviteCreate(ApiModel):
    identity_id: str
    quota_payer_user_id: str | None = None


class IdentityAssignmentDecision(ApiModel):
    decision: Literal["accepted", "rejected"]


class IdentityAssignmentView(ApiModel):
    id: str
    identity_id: str
    campaign_id: str
    soul_release_id: str
    role: str
    status: str
    invited_by_user_id: str
    quota_payer_user_id: str
    memory_namespace: str
    responded_at: datetime | None
    revoked_at: datetime | None


class IdentityMemoryUpsert(ApiModel):
    content: str = Field(min_length=1, max_length=20_000)
    audience: Literal["dm", "players", "public"] = "dm"
    source: Literal["curated", "agent_summary", "campaign_event"] = "curated"
    expected_revision: int | None = Field(default=None, ge=1)


class IdentityMemoryView(ApiModel):
    id: str
    assignment_id: str
    memory_key: str
    content: str
    audience: str
    source: str
    revision: int
    updated_at: datetime
