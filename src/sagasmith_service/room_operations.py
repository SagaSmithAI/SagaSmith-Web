"""Host-owned durable MCP receipts and conservative presentation recovery."""
from __future__ import annotations

import hashlib
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from sagasmith_service.integrations.agent import AgentResult
from sagasmith_service.models import ActorBindingProjection, CampaignMessage, RoomOperation
from sagasmith_service.room_jobs import RoomJobError


class OperationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    call_id: UUID
    tool: str = Field(min_length=1, max_length=160)
    state: Literal["dispatched", "returned"]
    result: dict[str, Any] = Field(default_factory=dict)


def recover_operations(session: Session, *, job_id: str, run_id: str,
                       trigger: CampaignMessage) -> AgentResult | None:
    rows = session.scalars(select(RoomOperation).where(
        RoomOperation.job_id == job_id
    ).order_by(RoomOperation.created_at, RoomOperation.id)).all()
    if not rows:
        return None
    unknown = [row for row in rows if row.state != "returned"]
    audience: dict[str, Any] = {"kind": trigger.audience}
    if trigger.audience == "private":
        refs = session.scalars(select(ActorBindingProjection.actor_id).where(
            ActorBindingProjection.campaign_id == trigger.campaign_id,
            ActorBindingProjection.user_id.in_(trigger.audience_user_ids or []),
            ActorBindingProjection.can_view_private.is_(True),
            ActorBindingProjection.status == "active",
        )).all()
        if not refs:
            raise RoomJobError("recovery_audience_unavailable", "恢复受众需要重新确认。",
                               False, "auth")
        audience = {"kind": "actors", "actor_refs": sorted(set(refs))[:16]}
    receipts = []
    ids = []
    for row in rows:
        if row.state != "returned":
            continue
        receipt = {"tool": row.tool, **row.result}
        receipts.append(receipt)
        structured = row.result.get("structured_content")
        for _ in range(4):
            if not isinstance(structured, dict):
                break
            resolution = structured.get("resolution_id")
            if isinstance(resolution, str) and resolution and len(resolution) <= 160:
                if resolution not in ids:
                    ids.append(resolution)
                break
            structured = structured.get("result", structured.get("response"))
    blocks = [{"type": "resolution_ref", "block_id": f"recovered-{i}", "resolution_id": rid}
              for i, rid in enumerate(ids[:23])]
    blocks.append({"type": "prompt", "block_id": "recovery-status", "text":
                   "已恢复本次工具调用记录，未重新执行动作。请查看规则结果和房间面板；"
                   "待选择或失败的操作仍需处理。叙事生成未完成。" + (
                       "仍有操作结果未知，主持人可核对原回执；请勿重复执行该动作。"
                       if unknown else "")})
    fingerprint = hashlib.sha256("|".join(
        f"{row.id}:{row.state}" for row in rows
    ).encode()).hexdigest()[:16]
    return AgentResult(content="", request_id=None, model=None, prompt_tokens=0,
                       completion_tokens=0, usage_known=False,
                       tool_receipts=tuple(receipts), structured_output={
                           "schema": "sagasmith.room-turn/v1", "run_id": run_id,
                           "messages": [{"output_id": f"recovered-{fingerprint}",
                                         "audience": audience,
                                         "blocks": blocks}], "suggestions": [],
                       })
