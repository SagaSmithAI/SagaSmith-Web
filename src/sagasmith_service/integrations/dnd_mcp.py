from __future__ import annotations

import json
from contextlib import AsyncExitStack
from typing import Any, Protocol

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


class DndRuntime(Protocol):
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

    async def import_content_pack(
        self,
        *,
        campaign_id: str,
        kind: str,
        source_path: str,
        principal_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]: ...


def _tool_payload(result: Any) -> dict[str, Any]:
    if getattr(result, "isError", False):
        message = "D&D MCP rejected the request"
        content = getattr(result, "content", [])
        if content and getattr(content[0], "text", None):
            message = content[0].text
        raise RuntimeError(message)
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for item in getattr(result, "content", []):
        text = getattr(item, "text", None)
        if not text:
            continue
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    raise RuntimeError("D&D MCP returned no structured receipt")


class StreamableHttpDndRuntime:
    """Thin client for public D&D MCP tools; it never reaches into MCP storage."""

    def __init__(self, url: str, *, bearer_token: str | None = None) -> None:
        self.url = url
        self.bearer_token = bearer_token

    async def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        headers = {}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        async with AsyncExitStack() as stack:
            client = await stack.enter_async_context(
                httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(30, connect=10))
            )
            read, write, _ = await stack.enter_async_context(
                streamable_http_client(self.url, http_client=client)
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            return _tool_payload(await session.call_tool(name, arguments=arguments))

    async def create_campaign(self, **arguments: Any) -> dict[str, Any]:
        return await self._call("campaign_create", arguments)

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
        )
