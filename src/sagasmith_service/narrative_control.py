"""Process-local Narrative MCP control operations for the hosted supervisor."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from sagasmith_service.auth_context import (
    AUTH_CONTEXT_META_KEY,
    AUTH_CONTEXT_RECEIPT_META_KEY,
    exposure_revision,
    sign_auth_context,
)
from sagasmith_service.mcp_result import is_tool_error, structured_tool_content

NarrativeOperation = Literal[
    "create_campaign",
    "get_campaign",
    "grant_campaign_access",
    "revoke_campaign_access",
    "grant_actor_access",
    "set_game_phase",
]

_REQUIRED_TOOLS = {"exposure", "campaign_query"}
_SESSION_ID = "service:narrative-control"
_CONVERSATION_PRINCIPAL = "service:narrative-control"


def _resolve_environment(value: str) -> str:
    resolved = os.path.expandvars(value)
    if resolved.startswith("${") and resolved.endswith("}"):
        raise ValueError(f"unresolved Narrative MCP environment value: {value}")
    return resolved


def _required_text(arguments: dict[str, Any], field: str) -> str:
    value = str(arguments.get(field) or "").strip()
    if not value:
        raise ValueError(f"{field} is required")
    return value


class NarrativeControlClient:
    """Run fixed hosted control operations through the real Narrative stdio MCP."""

    def __init__(self, *, parameters: StdioServerParameters, auth_context_secret: str) -> None:
        if len(auth_context_secret.encode("utf-8")) < 32:
            raise ValueError("Narrative auth context secret must contain at least 32 bytes")
        self.parameters = parameters
        self.auth_context_secret = auth_context_secret

    @classmethod
    def from_agent_config(cls, config_path: str) -> "NarrativeControlClient":
        path = Path(config_path).resolve()
        with path.open(encoding="utf-8") as source:
            config = json.load(source)
        servers = config.get("tools", {}).get("mcpServers", {})
        server = servers.get("sagasmith-narrative-mcp") or servers.get("sagasmith_narrative")
        if not isinstance(server, dict) or server.get("type") != "stdio":
            raise ValueError("Agent config must define sagasmith-narrative-mcp as stdio")
        command = _resolve_environment(str(server.get("command") or "").strip())
        if not command:
            raise ValueError("Narrative MCP command is required")
        raw_args = server.get("args") or []
        if not isinstance(raw_args, list) or not all(isinstance(item, str) for item in raw_args):
            raise ValueError("Narrative MCP args must be strings")
        raw_env = server.get("env") or {}
        if not isinstance(raw_env, dict):
            raise ValueError("Narrative MCP env must be an object")
        environment = dict(os.environ)
        environment.update(
            {
                str(key): _resolve_environment(str(value))
                for key, value in raw_env.items()
                if isinstance(key, str)
            }
        )
        secret = _resolve_environment(str(server.get("authContextSecret") or "").strip())
        if not secret:
            secret = environment.get("SAGASMITH_AUTH_CONTEXT_SECRET", "")
        cwd = _resolve_environment(str(server.get("cwd") or "").strip()) or None
        return cls(
            parameters=StdioServerParameters(
                command=command,
                args=[_resolve_environment(item) for item in raw_args],
                cwd=cwd,
                env=environment,
            ),
            auth_context_secret=secret,
        )

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[ClientSession]:
        async with stdio_client(self.parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    def _meta(
        self,
        *,
        actor_principal: str,
        campaign_id: str = "",
        authorization_epoch: int = 0,
    ) -> dict[str, Any]:
        return {
            AUTH_CONTEXT_META_KEY: sign_auth_context(
                secret=self.auth_context_secret,
                actor_principal=actor_principal,
                conversation_principal=_CONVERSATION_PRINCIPAL,
                campaign_id=campaign_id,
                session_id=_SESSION_ID,
                authorization_epoch=authorization_epoch,
            )
        }

    async def _call(
        self,
        session: ClientSession,
        tool: str,
        arguments: dict[str, Any],
        *,
        actor_principal: str,
        campaign_id: str = "",
        authorization_epoch: int = 0,
    ) -> dict[str, Any]:
        result = await session.call_tool(
            tool,
            arguments,
            meta=self._meta(
                actor_principal=actor_principal,
                campaign_id=campaign_id,
                authorization_epoch=authorization_epoch,
            ),
        )
        if is_tool_error(result):
            detail = result.content[0].text if result.content else f"Narrative tool {tool} failed"
            raise RuntimeError(detail)
        receipt = None
        for item in result.content:
            metadata = getattr(item, "meta", None)
            if isinstance(metadata, Mapping):
                candidate = metadata.get(AUTH_CONTEXT_RECEIPT_META_KEY)
                if isinstance(candidate, Mapping):
                    receipt = dict(candidate)
                    break
        structured = structured_tool_content(result)
        if isinstance(structured, dict):
            payload = dict(structured)
            if receipt is not None:
                payload["auth_context_receipt"] = receipt
            return payload
        for item in result.content:
            text = getattr(item, "text", None)
            if not isinstance(text, str):
                continue
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                if receipt is not None:
                    value["auth_context_receipt"] = receipt
                return value
        raise RuntimeError(f"Narrative tool {tool} returned no structured object")

    async def _bind(
        self,
        session: ClientSession,
        *,
        principal_id: str,
        campaign_id: str,
        tools: list[str],
    ) -> int:
        opened = await self._call(
            session,
            "exposure",
            {
                "action": "open",
                "campaign_id": campaign_id or None,
                "principal_id": principal_id,
            },
            actor_principal=principal_id,
            campaign_id=campaign_id,
        )
        epoch = exposure_revision(opened, 0)
        loaded = await self._call(
            session,
            "exposure",
            {
                "action": "set",
                "add_tool_ids": tools,
                "principal_id": principal_id,
            },
            actor_principal=principal_id,
            campaign_id=campaign_id,
            authorization_epoch=epoch,
        )
        return exposure_revision(loaded, epoch)

    async def probe(self) -> None:
        async with self._session() as session:
            names = {tool.name for tool in (await session.list_tools()).tools}
            missing = sorted(_REQUIRED_TOOLS - names)
            if missing:
                raise RuntimeError(
                    "Narrative MCP is missing required tools: " + ", ".join(missing)
                )
            await self._call(
                session,
                "exposure",
                {
                    "action": "open",
                    "principal_id": "service:narrative-readiness",
                },
                actor_principal="service:narrative-readiness",
            )

    async def execute(
        self, operation: NarrativeOperation, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        principal_id = _required_text(arguments, "principal_id")
        campaign_id = str(arguments.get("campaign_id") or "").strip()
        async with self._session() as session:
            if operation == "create_campaign":
                epoch = await self._bind(
                    session,
                    principal_id=principal_id,
                    campaign_id="",
                    tools=["campaign_setup"],
                )
                return await self._call(
                    session,
                    "campaign_setup",
                    {
                        "action": "create",
                        "name": _required_text(arguments, "name"),
                        "description": str(arguments.get("description") or ""),
                        "idempotency_key": _required_text(arguments, "idempotency_key"),
                        "principal_id": principal_id,
                    },
                    actor_principal=principal_id,
                    authorization_epoch=epoch,
                )

            campaign_id = _required_text(arguments, "campaign_id")
            if operation == "get_campaign":
                epoch = await self._bind(
                    session,
                    principal_id=principal_id,
                    campaign_id=campaign_id,
                    tools=["campaign_query"],
                )
                return await self._call(
                    session,
                    "campaign_query",
                    {
                        "action": "get",
                        "campaign_id": campaign_id,
                        "principal_id": principal_id,
                    },
                    actor_principal=principal_id,
                    campaign_id=campaign_id,
                    authorization_epoch=epoch,
                )

            if operation in {
                "grant_campaign_access",
                "revoke_campaign_access",
                "grant_actor_access",
            }:
                epoch = await self._bind(
                    session,
                    principal_id=principal_id,
                    campaign_id=campaign_id,
                    tools=["campaign_query", "branch_query", "access_change"],
                )
                campaign = await self._call(
                    session,
                    "campaign_query",
                    {
                        "action": "get",
                        "campaign_id": campaign_id,
                        "principal_id": principal_id,
                    },
                    actor_principal=principal_id,
                    campaign_id=campaign_id,
                    authorization_epoch=epoch,
                )
                branches = await self._call(
                    session,
                    "branch_query",
                    {"campaign_id": campaign_id, "principal_id": principal_id},
                    actor_principal=principal_id,
                    campaign_id=campaign_id,
                    authorization_epoch=epoch,
                )
                current = next(
                    (
                        item
                        for item in branches.get("branches", [])
                        if isinstance(item, dict) and item.get("is_current")
                    ),
                    None,
                )
                if current is None:
                    raise RuntimeError("Narrative campaign has no current branch")
                target = _required_text(arguments, "target_principal_id")
                if operation == "grant_campaign_access":
                    action = "campaign_grant"
                elif operation == "revoke_campaign_access":
                    action = "campaign_revoke"
                else:
                    action = (
                        "actor_grant"
                        if arguments.get("can_control") or arguments.get("can_view_private")
                        else "actor_revoke"
                    )
                return await self._call(
                    session,
                    "access_change",
                    {
                        "campaign_id": campaign_id,
                        "action": action,
                        "target_principal_id": target,
                        "role": arguments.get("role"),
                        "actor_id": arguments.get("actor_id"),
                        "can_control": bool(arguments.get("can_control")),
                        "can_view_private": bool(arguments.get("can_view_private")),
                        "expected_revision": int(campaign.get("revision") or 0),
                        "expected_branch_id": str(current["id"]),
                        "idempotency_key": _required_text(arguments, "idempotency_key"),
                        "principal_id": principal_id,
                    },
                    actor_principal=principal_id,
                    campaign_id=campaign_id,
                    authorization_epoch=epoch,
                )

            if operation == "set_game_phase":
                epoch = await self._bind(
                    session,
                    principal_id=principal_id,
                    campaign_id=campaign_id,
                    tools=["branch_query", "game_phase"],
                )
                branches = await self._call(
                    session,
                    "branch_query",
                    {"campaign_id": campaign_id, "principal_id": principal_id},
                    actor_principal=principal_id,
                    campaign_id=campaign_id,
                    authorization_epoch=epoch,
                )
                current = next(
                    (
                        item
                        for item in branches.get("branches", [])
                        if isinstance(item, dict) and item.get("is_current")
                    ),
                    None,
                )
                if current is None:
                    raise RuntimeError("Narrative campaign has no current branch")
                return await self._call(
                    session,
                    "game_phase",
                    {
                        "campaign_id": campaign_id,
                        "phase": _required_text(arguments, "phase"),
                        "expected_revision": int(arguments.get("expected_revision") or 0),
                        "expected_branch_id": str(current["id"]),
                        "idempotency_key": _required_text(arguments, "idempotency_key"),
                        "principal_id": principal_id,
                    },
                    actor_principal=principal_id,
                    campaign_id=campaign_id,
                    authorization_epoch=epoch,
                )

        raise ValueError(f"unsupported Narrative operation: {operation}")
