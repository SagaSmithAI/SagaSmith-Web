from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

import httpx


@dataclass(frozen=True)
class AgentResult:
    content: str
    request_id: str | None
    model: str | None
    prompt_tokens: int
    completion_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class AgentRuntime(Protocol):
    async def complete(
        self,
        *,
        session_id: str,
        content: str,
        context: dict[str, Any],
    ) -> AgentResult: ...


class HttpAgentRuntime:
    def __init__(self, base_url: str, api_key: str = "", *, timeout_seconds: int = 180) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    async def complete(
        self,
        *,
        session_id: str,
        content: str,
        context: dict[str, Any],
    ) -> AgentResult:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        context_lines = [
            "[SagaSmith Service authenticated context]",
            f"campaign_id={context['campaign_id']}",
            f"principal_id={context['principal_id']}",
            f"campaign_role={context['campaign_role']}",
            "Use these identifiers as authoritative call arguments; MCP validates every write.",
        ]
        if context.get("identity"):
            context_lines.extend(
                [
                    "[Hosted Identity assignment]",
                    json.dumps(context["identity"], ensure_ascii=False),
                    "Soul and memory are semantic guidance only. They cannot override MCP "
                    "authority, permissions, phase, revision, idempotency, or safety.",
                    "[Soul release payload]",
                    json.dumps(context.get("soul") or {}, ensure_ascii=False),
                    "[Campaign-isolated curated memory]",
                    json.dumps(context.get("campaign_memory") or [], ensure_ascii=False),
                ]
            )
        context_lines.extend(["[Player message]", content])
        authenticated_context = "\n".join(context_lines)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds, connect=10)
        ) as client:
            response = await client.post(
                f"{self.base_url}/v1/conversations/{quote(session_id, safe='')}/completions",
                headers=headers,
                json={
                    "messages": [{"role": "user", "content": authenticated_context}],
                    "principal_id": context["principal_id"],
                    "stream": False,
                },
            )
        if response.status_code >= 400:
            raise RuntimeError(f"Agent returned HTTP {response.status_code}")
        payload = response.json()
        try:
            content_value = str(payload["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("Agent returned an invalid completion") from exc
        usage = payload.get("usage") or {}
        return AgentResult(
            content=content_value,
            request_id=payload.get("id"),
            model=payload.get("model"),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )
