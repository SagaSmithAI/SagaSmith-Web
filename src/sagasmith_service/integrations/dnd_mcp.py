from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from contextlib import AsyncExitStack
from typing import Any, Protocol

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


class DndRuntime(Protocol):
    async def probe(self) -> None: ...

    async def get_campaign(self, *, campaign_id: str, principal_id: str) -> dict[str, Any]: ...

    async def create_campaign(
        self,
        *,
        name: str,
        description: str,
        edition: str,
        locale: str,
        advancement_mode: str,
        principal_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]: ...

    async def grant_campaign_access(
        self,
        *,
        campaign_id: str,
        principal_id: str,
        role: str,
        by_principal_id: str,
    ) -> dict[str, Any]: ...

    async def grant_actor_access(
        self,
        *,
        campaign_id: str,
        actor_id: str,
        principal_id: str,
        can_control: bool,
        can_view_private: bool,
        by_principal_id: str,
    ) -> dict[str, Any]: ...

    async def revoke_campaign_access(
        self,
        *,
        campaign_id: str,
        principal_id: str,
        by_principal_id: str,
    ) -> dict[str, Any]: ...

    async def import_content_pack(
        self,
        *,
        campaign_id: str,
        kind: str,
        source_path: str,
        principal_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]: ...

    async def activate_content_pack(
        self,
        *,
        campaign_id: str,
        kind: str,
        runtime_ref: str,
        pack_id: str,
        version: str,
        principal_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]: ...

    async def module_draft(
        self,
        *,
        campaign_id: str,
        action: str,
        payload: dict[str, Any],
        principal_id: str,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]: ...

    async def import_content_artifact(
        self,
        *,
        campaign_id: str,
        artifact: str,
        principal_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]: ...

    async def get_content_artifact(
        self,
        *,
        campaign_id: str,
        artifact: str,
        principal_id: str,
    ) -> dict[str, Any]: ...

    async def get_panel_state(
        self, *, campaign_id: str, principal_id: str
    ) -> dict[str, Any]: ...

    async def get_character_card(
        self, *, campaign_id: str, character_id: str, principal_id: str
    ) -> dict[str, Any]: ...

    async def get_resolution_presentation(
        self, *, campaign_id: str, resolution_id: str, principal_id: str
    ) -> dict[str, Any]: ...

    async def set_game_phase(
        self,
        *,
        campaign_id: str,
        principal_id: str,
        tool_profile: str,
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]: ...

    async def start_combat(
        self,
        *,
        campaign_id: str,
        principal_id: str,
        participant_ids: list[str],
        positioning_mode: str,
        name: str,
        participant_config: list[dict[str, Any]],
        battle_map: dict[str, Any] | None,
        battle_map_override_reason: str | None,
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]: ...

    async def end_combat(
        self,
        *,
        campaign_id: str,
        principal_id: str,
        outcome: dict[str, Any],
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]: ...


def _tool_payload(result: Any) -> dict[str, Any]:
    if getattr(result, "isError", False):
        message = "D&D MCP rejected the request"
        content = getattr(result, "content", [])
        if content and getattr(content[0], "text", None):
            message = content[0].text
        raise RuntimeError(message)
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
        if not text:
            continue
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            if receipt is not None:
                parsed["auth_context_receipt"] = receipt
            return parsed
    raise RuntimeError("D&D MCP returned no structured receipt")


def _runtime_error(error: BaseException) -> RuntimeError:
    if isinstance(error, RuntimeError):
        return error
    if isinstance(error, BaseExceptionGroup):
        for child in error.exceptions:
            mapped = _runtime_error(child)
            if str(mapped) != "D&D MCP request failed":
                return mapped
    return RuntimeError("D&D MCP request failed")


class StreamableHttpDndRuntime:
    """Thin client for public D&D MCP tools; it never reaches into MCP storage."""

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
                    system="dnd5e",
                    operation_class="probe",
                    transport="streamable_http",
                ):
                    read, write, _ = await stack.enter_async_context(
                        streamable_http_client(self.url, http_client=self.http_client)
                    )
                session = await stack.enter_async_context(ClientSession(read, write))
                with observe_latency(
                    MCP_INITIALIZE_SECONDS,
                    system="dnd5e",
                    operation_class="probe",
                    transport="streamable_http",
                ):
                    await session.initialize()
                with observe_latency(
                    MCP_TOOL_SECONDS,
                    system="dnd5e",
                    operation_class="probe",
                    transport="streamable_http",
                ):
                    available = {tool.name for tool in (await session.list_tools()).tools}
        except Exception as exc:
            raise RuntimeError("D&D MCP readiness probe failed") from exc
        missing = sorted(required - available)
        if missing:
            raise RuntimeError(f"D&D MCP is missing required tools: {', '.join(missing)}")

    async def _call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        exposure_principal: str,
        campaign_id: str | None,
    ) -> dict[str, Any]:
        try:
            async with AsyncExitStack() as stack:
                with observe_latency(
                    MCP_TRANSPORT_SETUP_SECONDS,
                    system="dnd5e",
                    operation_class="request",
                    transport="streamable_http",
                ):
                    read, write, _ = await stack.enter_async_context(
                        streamable_http_client(self.url, http_client=self.http_client)
                    )
                session = await stack.enter_async_context(ClientSession(read, write))
                with observe_latency(
                    MCP_INITIALIZE_SECONDS,
                    system="dnd5e",
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
                                actor_principal=exposure_principal,
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
                    system="dnd5e",
                    operation_class="request",
                    transport="streamable_http",
                ):
                    exposure_args: dict[str, Any] = {
                        "action": "open",
                        "principal_id": exposure_principal,
                    }
                    if campaign_id is not None:
                        exposure_args["campaign_id"] = campaign_id
                    await call("exposure", exposure_args)
                    search = await call(
                        "exposure",
                        {
                            "action": "search",
                            "campaign_id": campaign_id,
                            "principal_id": exposure_principal,
                            "query": name,
                        },
                    )
                    matched_tools = {
                        str(item.get("tool_id") or "") for item in search.get("matches", [])
                    }
                    visible_tools = {str(item) for item in search.get("visible_tools", [])}
                    if name not in matched_tools and name not in visible_tools:
                        raise RuntimeError(
                            f"D&D MCP does not expose {name!r} in the current context: {search}"
                        )
                    if name not in visible_tools:
                        await call(
                            "exposure",
                            {
                                "action": "set",
                                "campaign_id": campaign_id,
                                "principal_id": exposure_principal,
                                "add_tool_ids": [name],
                            },
                        )
                    listed = await session.list_tools()
                    if name not in {tool.name for tool in listed.tools}:
                        raise RuntimeError(
                            f"D&D MCP did not publish {name!r} after exposure update"
                        )
                with observe_latency(
                    MCP_TOOL_SECONDS,
                    system="dnd5e",
                    operation_class="request",
                    transport="streamable_http",
                ):
                    return await call(name, arguments)
        except Exception as exc:
            raise _runtime_error(exc) from exc

    async def create_campaign(self, **arguments: Any) -> dict[str, Any]:
        return await self._call(
            "campaign_create",
            arguments,
            exposure_principal=arguments["principal_id"],
            campaign_id=None,
        )

    async def get_campaign(self, **arguments: Any) -> dict[str, Any]:
        return await self._call(
            "campaign_query",
            {
                "view": "get",
                "payload": {"campaign_id": arguments["campaign_id"]},
                "principal_id": arguments["principal_id"],
            },
            exposure_principal=arguments["principal_id"],
            campaign_id=arguments["campaign_id"],
        )

    async def grant_campaign_access(self, **arguments: Any) -> dict[str, Any]:
        return await self._call(
            "access_grant",
            {
                "scope": "campaign",
                "campaign_id": arguments["campaign_id"],
                "principal_id": arguments["principal_id"],
                "payload": {"role": arguments["role"]},
                "by_principal_id": arguments["by_principal_id"],
            },
            exposure_principal=arguments["by_principal_id"],
            campaign_id=arguments["campaign_id"],
        )

    async def grant_actor_access(self, **arguments: Any) -> dict[str, Any]:
        return await self._call(
            "access_grant",
            {
                "scope": "actor",
                "campaign_id": arguments["campaign_id"],
                "principal_id": arguments["principal_id"],
                "payload": {
                    "actor_id": arguments["actor_id"],
                    "can_control": arguments["can_control"],
                    "can_view_private": arguments["can_view_private"],
                },
                "by_principal_id": arguments["by_principal_id"],
            },
            exposure_principal=arguments["by_principal_id"],
            campaign_id=arguments["campaign_id"],
        )

    async def revoke_campaign_access(self, **arguments: Any) -> dict[str, Any]:
        return await self._call(
            "access_revoke",
            {
                "campaign_id": arguments["campaign_id"],
                "principal_id": arguments["principal_id"],
                "by_principal_id": arguments["by_principal_id"],
            },
            exposure_principal=arguments["by_principal_id"],
            campaign_id=arguments["campaign_id"],
        )

    async def import_content_pack(self, **arguments: Any) -> dict[str, Any]:
        return await self._call(
            "content_pack",
            {
                "action": "import",
                "payload": {
                    "campaign_id": arguments["campaign_id"],
                    "kind": arguments["kind"],
                    "source_path": arguments["source_path"],
                },
                "principal_id": arguments["principal_id"],
                "idempotency_key": arguments["idempotency_key"],
            },
            exposure_principal=arguments["principal_id"],
            campaign_id=arguments["campaign_id"],
        )

    async def activate_content_pack(self, **arguments: Any) -> dict[str, Any]:
        kind = arguments["kind"]
        payload: dict[str, Any] = {
            "campaign_id": arguments["campaign_id"],
            "kind": kind,
        }
        if kind == "module":
            payload["module_id"] = arguments["runtime_ref"]
        elif kind == "addon":
            payload.update({"addon_id": arguments["runtime_ref"], "version": arguments["version"]})
        elif kind == "core_rules":
            payload.update({"pack_id": arguments["runtime_ref"], "version": arguments["version"]})
        else:
            raise RuntimeError(f"D&D Pack kind {kind!r} does not support activation")
        return await self._call(
            "content_pack",
            {
                "action": "activate",
                "payload": payload,
                "principal_id": arguments["principal_id"],
                "idempotency_key": arguments["idempotency_key"],
            },
            exposure_principal=arguments["principal_id"],
            campaign_id=arguments["campaign_id"],
        )

    async def module_draft(self, **arguments: Any) -> dict[str, Any]:
        call_arguments: dict[str, Any] = {
            "campaign_id": arguments["campaign_id"],
            "action": arguments["action"],
            "payload": arguments.get("payload") or {},
            "principal_id": arguments["principal_id"],
        }
        if arguments.get("expected_revision") is not None:
            call_arguments["expected_revision"] = arguments["expected_revision"]
        if arguments.get("idempotency_key"):
            call_arguments["idempotency_key"] = arguments["idempotency_key"]
        return await self._call(
            "module_draft",
            call_arguments,
            exposure_principal=arguments["principal_id"],
            campaign_id=arguments["campaign_id"],
        )

    async def import_content_artifact(self, **arguments: Any) -> dict[str, Any]:
        return await self._call(
            "content_pack",
            {
                "action": "import",
                "payload": {
                    "campaign_id": arguments["campaign_id"],
                    "kind": "module",
                    "artifact": arguments["artifact"],
                },
                "principal_id": arguments["principal_id"],
                "idempotency_key": arguments["idempotency_key"],
            },
            exposure_principal=arguments["principal_id"],
            campaign_id=arguments["campaign_id"],
        )

    async def get_content_artifact(self, **arguments: Any) -> dict[str, Any]:
        return await self._call(
            "content_pack",
            {
                "action": "get",
                "payload": {
                    "campaign_id": arguments["campaign_id"],
                    "kind": "module",
                    "artifact": arguments["artifact"],
                },
                "principal_id": arguments["principal_id"],
            },
            exposure_principal=arguments["principal_id"],
            campaign_id=arguments["campaign_id"],
        )

    @staticmethod
    def _result(value: dict[str, Any]) -> Any:
        return value.get("result", value)

    async def get_panel_state(self, **arguments: Any) -> dict[str, Any]:
        campaign_id = arguments["campaign_id"]
        principal_id = arguments["principal_id"]
        campaign_receipt = await self.get_campaign(
            campaign_id=campaign_id, principal_id=principal_id
        )
        campaign = self._result(campaign_receipt)
        phase = str(campaign.get("effective_game_phase") or "lobby")
        party_receipt = await self._call(
            "campaign_query",
            {
                "view": "party",
                "payload": {"campaign_id": campaign_id},
                "principal_id": principal_id,
            },
            exposure_principal=principal_id,
            campaign_id=campaign_id,
        )
        characters_receipt = await self._call(
            "character_query",
            {
                "view": "list",
                "payload": {"campaign_id": campaign_id},
                "principal_id": principal_id,
            },
            exposure_principal=principal_id,
            campaign_id=campaign_id,
        )
        modules_receipt = await self._call(
            "module_query",
            {"campaign_id": campaign_id, "view": "list", "principal_id": principal_id},
            exposure_principal=principal_id,
            campaign_id=campaign_id,
        )
        current_module: Any = None
        try:
            current_receipt = await self._call(
                "module_query",
                {"campaign_id": campaign_id, "view": "current", "principal_id": principal_id},
                exposure_principal=principal_id,
                campaign_id=campaign_id,
            )
            current_module = self._result(current_receipt)
        except RuntimeError:
            current_module = None
        combat: Any = None
        if phase == "combat":
            combat_receipt = await self._call(
                "combat_query",
                {"campaign_id": campaign_id, "view": "status", "principal_id": principal_id},
                exposure_principal=principal_id,
                campaign_id=campaign_id,
            )
            combat = self._result(combat_receipt)
        return {
            "campaign": campaign,
            "phase": phase,
            "revision": int(campaign.get("revision") or campaign.get("campaign_revision") or 0),
            "party": self._result(party_receipt),
            "characters": self._result(characters_receipt),
            "modules": self._result(modules_receipt),
            "current_module": current_module,
            "combat": combat,
        }

    async def get_character_card(self, **arguments: Any) -> dict[str, Any]:
        campaign_id = arguments["campaign_id"]
        receipt = await self._call(
            "character_query",
            {
                "view": "get",
                "payload": {"character_id": arguments["character_id"]},
                "principal_id": arguments["principal_id"],
            },
            exposure_principal=arguments["principal_id"],
            campaign_id=campaign_id,
        )
        character = self._result(receipt)
        if not isinstance(character, dict) or str(character.get("campaign_id") or "") != str(
            campaign_id
        ):
            raise RuntimeError("D&D MCP returned a character outside the campaign")
        return character

    async def get_resolution_presentation(self, **arguments: Any) -> dict[str, Any]:
        receipt = await self._call(
            "resolution_presentation",
            {
                "campaign_id": arguments["campaign_id"],
                "resolution_id": arguments["resolution_id"],
                "principal_id": arguments["principal_id"],
            },
            exposure_principal=arguments["principal_id"],
            campaign_id=arguments["campaign_id"],
        )
        value: Any = receipt
        for _ in range(3):
            if not isinstance(value, dict) or "result" not in value:
                break
            value = value["result"]
        if not isinstance(value, dict):
            raise RuntimeError("D&D MCP returned no resolution presentation")
        return value

    async def set_game_phase(self, **arguments: Any) -> dict[str, Any]:
        return await self._call(
            "game_phase",
            {
                "campaign_id": arguments["campaign_id"],
                "action": "set",
                "tool_profile": arguments["tool_profile"],
                "principal_id": arguments["principal_id"],
                "expected_revision": arguments["expected_revision"],
                "idempotency_key": arguments["idempotency_key"],
            },
            exposure_principal=arguments["principal_id"],
            campaign_id=arguments["campaign_id"],
        )

    async def start_combat(self, **arguments: Any) -> dict[str, Any]:
        call_arguments = {
            "campaign_id": arguments["campaign_id"],
            "participant_ids": arguments["participant_ids"],
            "participant_config": arguments.get("participant_config") or [],
            "positioning_mode": arguments["positioning_mode"],
            "name": arguments["name"],
            "principal_id": arguments["principal_id"],
            "expected_revision": arguments["expected_revision"],
            "idempotency_key": arguments["idempotency_key"],
        }
        if arguments.get("battle_map") is not None:
            call_arguments["battle_map"] = arguments["battle_map"]
        if arguments.get("battle_map_override_reason"):
            call_arguments["battle_map_override_reason"] = arguments[
                "battle_map_override_reason"
            ]
        return await self._call(
            "combat_start",
            call_arguments,
            exposure_principal=arguments["principal_id"],
            campaign_id=arguments["campaign_id"],
        )

    async def end_combat(self, **arguments: Any) -> dict[str, Any]:
        return await self._call(
            "combat_end",
            {
                "campaign_id": arguments["campaign_id"],
                "outcome": arguments["outcome"],
                "principal_id": arguments["principal_id"],
                "expected_revision": arguments["expected_revision"],
                "idempotency_key": arguments["idempotency_key"],
            },
            exposure_principal=arguments["principal_id"],
            campaign_id=arguments["campaign_id"],
        )
