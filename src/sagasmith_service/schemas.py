from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


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


class ConversationView(ApiModel):
    id: str
    campaign_id: str
    user_id: str
    title: str
    status: str


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
