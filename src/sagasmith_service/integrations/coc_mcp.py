from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from contextlib import AsyncExitStack
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from sagasmith_service.auth_context import (
    AUTH_CONTEXT_META_KEY,
    AUTH_CONTEXT_RECEIPT_META_KEY,
    exposure_revision,
    sign_auth_context,
)
from sagasmith_service.observability import (
    MCP_EXPOSURE_SECONDS,
    MCP_INITIALIZE_SECONDS,
    MCP_TOOL_SECONDS,
    MCP_TRANSPORT_SETUP_SECONDS,
    observe_latency,
)


def _tool_payload(result: Any) -> dict[str, Any]:
    if getattr(result, "isError", False):
        content = getattr(result, "content", [])
        message = getattr(content[0], "text", None) if content else None
        raise RuntimeError(message or "CoC MCP rejected the request")
    receipt = None
    for item in getattr(result, "content", []):
        metadata = getattr(item, "meta", None)
        if isinstance(metadata, Mapping):
            candidate = metadata.get(AUTH_CONTEXT_RECEIPT_META_KEY)
            if isinstance(candidate, Mapping):
                receipt = dict(candidate)
                break
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        payload = dict(structured)
        if receipt is not None:
            payload["auth_context_receipt"] = receipt
        return payload
    for item in getattr(result, "content", []):
        text = getattr(item, "text", None)
        if text:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                if receipt is not None:
                    parsed["auth_context_receipt"] = receipt
                return parsed
    raise RuntimeError("CoC MCP returned no structured receipt")


class StreamableHttpCocRuntime:
    """Narrow hosted adapter over CoC's native dynamic MCP tools."""

    def __init__(
        self,
        url: str,
        *,
        bearer_token: str | None = None,
        auth_context_secret: str = "",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = url
        self.bearer_token = bearer_token
        self.auth_context_secret = auth_context_secret
        self._owns_http_client = http_client is None
        headers = (
            {"Authorization": f"Bearer {self.bearer_token}"}
            if self.bearer_token
            else {}
        )
        self.http_client = (
            http_client
            if http_client is not None
            else httpx.AsyncClient(
                headers=headers,
                timeout=httpx.Timeout(30, connect=10),
            )
        )
        if http_client is not None and headers:
            self.http_client.headers.update(headers)

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self.http_client.aclose()

    async def probe(self) -> None:
        """Verify the MCP endpoint and the hosted contract without mutating state."""
        required = {
            "exposure",
            "server_capabilities",
            "storage_status",
            "campaign_query",
            "game_phase",
            "skill_query",
            "resolution_presentation",
        }
        try:
            async with AsyncExitStack() as stack:
                with observe_latency(
                    MCP_TRANSPORT_SETUP_SECONDS,
                    system="coc7e",
                    operation_class="probe",
                    transport="streamable_http",
                ):
                    read, write, _ = await stack.enter_async_context(
                        streamable_http_client(self.url, http_client=self.http_client)
                    )
                session = await stack.enter_async_context(ClientSession(read, write))
                with observe_latency(
                    MCP_INITIALIZE_SECONDS,
                    system="coc7e",
                    operation_class="probe",
                    transport="streamable_http",
                ):
                    await session.initialize()
                with observe_latency(
                    MCP_TOOL_SECONDS,
                    system="coc7e",
                    operation_class="probe",
                    transport="streamable_http",
                ):
                    available = {tool.name for tool in (await session.list_tools()).tools}
        except Exception as exc:
            raise RuntimeError("CoC MCP readiness probe failed") from exc
        missing = sorted(required - available)
        if missing:
            raise RuntimeError(f"CoC MCP is missing required tools: {', '.join(missing)}")

    @staticmethod
    def _result(value: dict[str, Any]) -> dict[str, Any]:
        current: Any = value
        for _ in range(3):
            if not isinstance(current, dict) or "result" not in current:
                break
            current = current["result"]
        if not isinstance(current, dict):
            raise RuntimeError("CoC MCP returned no structured result")
        return current

    async def _call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        principal_id: str,
        campaign_id: str | None,
    ) -> dict[str, Any]:
        try:
            async with AsyncExitStack() as stack:
                with observe_latency(
                    MCP_TRANSPORT_SETUP_SECONDS,
                    system="coc7e",
                    operation_class="request",
                    transport="streamable_http",
                ):
                    read, write, _ = await stack.enter_async_context(
                        streamable_http_client(self.url, http_client=self.http_client)
                    )
                session = await stack.enter_async_context(ClientSession(read, write))
                with observe_latency(
                    MCP_INITIALIZE_SECONDS,
                    system="coc7e",
                    operation_class="request",
                    transport="streamable_http",
                ):
                    await session.initialize()
                auth_session_id = f"service:{uuid.uuid4().hex}"
                conversation_principal = (
                    f"service:campaign:{campaign_id}" if campaign_id else "service:control-plane"
                )
                authorization_epoch = 0

                async def call(tool_name: str, tool_arguments: dict[str, Any]):
                    nonlocal authorization_epoch
                    metadata = None
                    if self.auth_context_secret:
                        metadata = {
                            AUTH_CONTEXT_META_KEY: sign_auth_context(
                                secret=self.auth_context_secret,
                                actor_principal=principal_id,
                                conversation_principal=conversation_principal,
                                session_id=auth_session_id,
                                campaign_id=campaign_id or "",
                                authorization_epoch=authorization_epoch,
                            )
                        }
                    result = await session.call_tool(
                        tool_name,
                        arguments=tool_arguments,
                        **({"meta": metadata} if metadata is not None else {}),
                    )
                    payload = _tool_payload(result)
                    authorization_epoch = exposure_revision(payload, authorization_epoch)
                    return payload

                with observe_latency(
                    MCP_EXPOSURE_SECONDS,
                    system="coc7e",
                    operation_class="request",
                    transport="streamable_http",
                ):
                    exposure: dict[str, Any] = {
                        "action": "open",
                        "principal_id": principal_id,
                    }
                    if campaign_id is not None:
                        exposure["campaign_id"] = campaign_id
                    await call("exposure", exposure)
                    if name != "exposure":
                        search = await call(
                            "exposure",
                            {
                                "action": "search",
                                "campaign_id": campaign_id,
                                "principal_id": principal_id,
                                "query": name,
                            },
                        )
                        visible = {str(item) for item in search.get("visible_tools", [])}
                        if name not in visible:
                            await call(
                                "exposure",
                                {
                                    "action": "set",
                                    "campaign_id": campaign_id,
                                    "principal_id": principal_id,
                                    "add_tool_ids": [name],
                                },
                            )
                        listed = await session.list_tools()
                        if name not in {tool.name for tool in listed.tools}:
                            raise RuntimeError(f"CoC MCP did not expose {name!r}")
                with observe_latency(
                    MCP_TOOL_SECONDS,
                    system="coc7e",
                    operation_class="request",
                    transport="streamable_http",
                ):
                    return await call(name, arguments)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("CoC MCP request failed") from exc

    async def create_campaign(self, **arguments: Any) -> dict[str, Any]:
        return await self._call(
            "campaign_change",
            {
                "action": "create",
                "data": {
                    "name": arguments["name"],
                    "description": arguments.get("description", ""),
                    "idempotency_key": arguments["idempotency_key"],
                },
                "principal_id": arguments["principal_id"],
            },
            principal_id=arguments["principal_id"],
            campaign_id=None,
        )

    async def get_campaign(self, **arguments: Any) -> dict[str, Any]:
        return await self._call(
            "campaign_query",
            {
                "action": "get",
                "campaign_id": arguments["campaign_id"],
                "principal_id": arguments["principal_id"],
            },
            principal_id=arguments["principal_id"],
            campaign_id=arguments["campaign_id"],
        )

    async def grant_campaign_access(self, **arguments: Any) -> dict[str, Any]:
        return await self._call(
            "campaign_change",
            {
                "action": "grant_campaign",
                "campaign_id": arguments["campaign_id"],
                "data": {
                    "target_principal_id": arguments["principal_id"],
                    "role": arguments["role"],
                },
                "principal_id": arguments["by_principal_id"],
            },
            principal_id=arguments["by_principal_id"],
            campaign_id=arguments["campaign_id"],
        )

    async def grant_actor_access(self, **arguments: Any) -> dict[str, Any]:
        return await self._call(
            "campaign_change",
            {
                "action": "grant_actor",
                "campaign_id": arguments["campaign_id"],
                "data": {
                    "target_principal_id": arguments["principal_id"],
                    "actor_id": arguments["actor_id"],
                    "can_control": arguments["can_control"],
                    "can_view_private": arguments["can_view_private"],
                },
                "principal_id": arguments["by_principal_id"],
            },
            principal_id=arguments["by_principal_id"],
            campaign_id=arguments["campaign_id"],
        )

    async def revoke_campaign_access(self, **arguments: Any) -> dict[str, Any]:
        return await self._call(
            "campaign_change",
            {
                "action": "revoke_campaign",
                "campaign_id": arguments["campaign_id"],
                "data": {"target_principal_id": arguments["principal_id"]},
                "principal_id": arguments["by_principal_id"],
            },
            principal_id=arguments["by_principal_id"],
            campaign_id=arguments["campaign_id"],
        )

    async def get_character_card(self, **arguments: Any) -> dict[str, Any]:
        receipt = await self._call(
            "character_query",
            {
                "action": "get",
                "campaign_id": arguments["campaign_id"],
                "character_id": arguments["character_id"],
                "principal_id": arguments["principal_id"],
            },
            principal_id=arguments["principal_id"],
            campaign_id=arguments["campaign_id"],
        )
        character = self._result(receipt)
        if str(character.get("campaign_id") or "") != str(arguments["campaign_id"]):
            raise RuntimeError("CoC MCP returned a character outside the campaign")
        return character

    async def get_resolution_presentation(self, **arguments: Any) -> dict[str, Any]:
        receipt = await self._call(
            "resolution_presentation",
            {
                "campaign_id": arguments["campaign_id"],
                "resolution_id": arguments["resolution_id"],
                "principal_id": arguments["principal_id"],
            },
            principal_id=arguments["principal_id"],
            campaign_id=arguments["campaign_id"],
        )
        return self._result(receipt)

    async def get_panel_state(self, **arguments: Any) -> dict[str, Any]:
        campaign = self._result(await self.get_campaign(**arguments))
        campaign_id = arguments["campaign_id"]
        principal_id = arguments["principal_id"]
        characters = self._result(
            await self._call(
            "character_query",
            {"action": "list", "campaign_id": campaign_id, "principal_id": principal_id},
            principal_id=principal_id,
            campaign_id=campaign_id,
            )
        )
        state = dict(campaign.get("state") or {})
        phase = str(campaign.get("effective_game_phase") or state.get("game_phase") or "lobby")
        return {
            "campaign": campaign,
            "phase": phase,
            "revision": int(campaign.get("revision") or 0),
            "party": {},
            "characters": list(characters.get("characters") or []),
            "modules": [],
            "current_module": None,
            "combat": state.get("combat"),
            "chase": state.get("chase"),
        }

    async def set_game_phase(self, **arguments: Any) -> dict[str, Any]:
        return await self._call(
            "campaign_change",
            {
                "action": "set_phase",
                "campaign_id": arguments["campaign_id"],
                "data": {
                    "phase": arguments["tool_profile"],
                    "expected_revision": arguments["expected_revision"],
                    "idempotency_key": arguments["idempotency_key"],
                },
                "principal_id": arguments["principal_id"],
            },
            principal_id=arguments["principal_id"],
            campaign_id=arguments["campaign_id"],
        )

    async def start_combat(self, **arguments: Any) -> dict[str, Any]:
        configs = {
            str(item.get("actor_id") or ""): dict(item)
            for item in arguments.get("participant_config") or []
        }
        participants: list[dict[str, Any]] = []
        revisions: dict[str, int] = {}
        for actor_id in arguments["participant_ids"]:
            card = await self.get_character_card(
                campaign_id=arguments["campaign_id"],
                character_id=actor_id,
                principal_id=arguments["principal_id"],
            )
            revisions[actor_id] = int(card.get("revision") or 0)
            config = configs.get(actor_id, {})
            participant = {
                "actor_id": actor_id,
                "side": str(config.get("side") or "investigators"),
                "ready_firearm": bool(config.get("ready_firearm", False)),
            }
            if arguments["positioning_mode"] == "grid":
                participant["position"] = config.get("position", [0, 0])
            participants.append(participant)
        return await self._call(
            "combat_start",
            {
                "campaign_id": arguments["campaign_id"],
                "participants": participants,
                "expected_character_revisions": revisions,
                "positioning_mode": arguments["positioning_mode"],
                "source": arguments.get("name") or "Hosted Keeper combat start",
                "expected_revision": arguments["expected_revision"],
                "idempotency_key": arguments["idempotency_key"],
                "principal_id": arguments["principal_id"],
            },
            principal_id=arguments["principal_id"],
            campaign_id=arguments["campaign_id"],
        )

    async def end_combat(self, **arguments: Any) -> dict[str, Any]:
        raw_outcome = arguments.get("outcome") or {}
        outcome = str(raw_outcome.get("code") or raw_outcome.get("outcome") or "other")
        if outcome not in {"victory", "escape", "surrender", "defeat", "other"}:
            outcome = "other"
        return await self._call(
            "combat_end",
            {
                "campaign_id": arguments["campaign_id"],
                "outcome": outcome,
                "source": str(raw_outcome.get("source") or "Hosted Keeper combat end"),
                "expected_revision": arguments["expected_revision"],
                "idempotency_key": arguments["idempotency_key"],
                "principal_id": arguments["principal_id"],
            },
            principal_id=arguments["principal_id"],
            campaign_id=arguments["campaign_id"],
        )
