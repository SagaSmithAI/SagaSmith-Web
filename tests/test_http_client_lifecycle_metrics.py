from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from sagasmith_service.api.rooms import _observed_projection_batch
from sagasmith_service.config import Settings
from sagasmith_service.database import make_engine
from sagasmith_service.integrations.agent import HttpAgentRuntime
from sagasmith_service.integrations.dnd_mcp import StreamableHttpDndRuntime
from sagasmith_service.main import create_app
from sagasmith_service.observability import (
    AGENT_UPSTREAM_SECONDS,
    MCP_EXPOSURE_SECONDS,
    MCP_INITIALIZE_SECONDS,
    MCP_TOOL_SECONDS,
    MCP_TRANSPORT_SETUP_SECONDS,
    ROOM_PROJECTION_BATCH_SECONDS,
    ROOM_PROJECTION_JOBS,
)


def _sample(name: str, labels: dict[str, str]) -> float:
    return float(REGISTRY.get_sample_value(name, labels) or 0)


def test_app_lifespan_owns_and_closes_each_default_http_client(monkeypatch, tmp_path) -> None:
    created: list[Any] = []

    class TrackingClient:
        def __init__(self, **_kwargs: object) -> None:
            self.closed = False
            self.fail_close = len(created) == 0
            created.append(self)

        async def aclose(self) -> None:
            self.closed = True
            if self.fail_close:
                raise RuntimeError("simulated close failure")

    monkeypatch.setattr("sagasmith_service.main.httpx.AsyncClient", TrackingClient)
    database_url = f"sqlite:///{(tmp_path / 'http-lifecycle.db').as_posix()}"
    app = create_app(
        Settings(env="test", database_url=database_url),
        make_engine(database_url),
    )

    assert list(app.state.outbound_http_clients) == ["dnd", "coc", "narrative", "agent"]
    assert app.state.dnd_runtime.http_client is app.state.outbound_http_clients["dnd"]
    assert app.state.coc_runtime.http_client is app.state.outbound_http_clients["coc"]
    assert app.state.narrative_runtime.http_client is app.state.outbound_http_clients["narrative"]
    assert app.state.agent_runtime.http_client is app.state.outbound_http_clients["agent"]

    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200

    assert len(created) == 4
    assert all(client.closed for client in created)


def test_agent_runtime_reuses_injected_client_and_records_success_and_error() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if len(requests) == 2:
            return httpx.Response(
                200,
                json={
                    "id": "request-1",
                    "model": "test-model",
                    "choices": [{"message": {"content": "ready"}}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1},
                },
            )
        return httpx.Response(503, json={"detail": "unavailable"})

    labels = {
        "system": "agent",
        "operation_class": "completion",
        "status": "success",
        "transport": "http",
    }
    error_labels = {**labels, "status": "error"}
    success_before = _sample("sagasmith_agent_upstream_seconds_count", labels)
    error_before = _sample("sagasmith_agent_upstream_seconds_count", error_labels)

    async def exercise() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        runtime = HttpAgentRuntime("http://agent.test", http_client=client)
        await runtime.probe()
        result = await runtime.complete(
            session_id="conversation/1",
            content="hello",
            context={
                "campaign_id": "campaign-1",
                "principal_id": "user:1",
                "campaign_role": "owner",
            },
        )
        assert result.content == "ready"
        with pytest.raises(RuntimeError, match="HTTP 503"):
            await runtime.complete(
                session_id="conversation/1",
                content="again",
                context={
                    "campaign_id": "campaign-1",
                    "principal_id": "user:1",
                    "campaign_role": "owner",
                },
            )
        await runtime.aclose()
        assert not client.is_closed
        await client.aclose()

    asyncio.run(exercise())

    assert len(requests) == 3
    assert _sample("sagasmith_agent_upstream_seconds_count", labels) == success_before + 1
    assert _sample("sagasmith_agent_upstream_seconds_count", error_labels) == error_before + 1


def test_mcp_calls_reuse_http_pool_but_keep_sessions_isolated(monkeypatch) -> None:
    seen_clients: list[httpx.AsyncClient] = []
    sessions: list[Any] = []

    @asynccontextmanager
    async def fake_transport(_url: str, *, http_client: httpx.AsyncClient):
        seen_clients.append(http_client)
        yield object(), object(), lambda: None

    class FakeSession:
        async def __aenter__(self):
            sessions.append(self)
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def initialize(self) -> None:
            return None

        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name="campaign_query")])

        async def call_tool(
            self,
            _name: str,
            *,
            arguments: dict[str, Any],
            **_kwargs: object,
        ):
            action = arguments.get("action")
            if action == "search":
                payload = {
                    "matches": [{"tool_id": "campaign_query"}],
                    "visible_tools": ["campaign_query"],
                }
            elif action in {"open", "set"}:
                payload = {"visible_tools": ["campaign_query"]}
            else:
                payload = {"result": {"id": "campaign-1"}}
            return SimpleNamespace(isError=False, content=[], structuredContent=payload)

    monkeypatch.setattr(
        "sagasmith_service.integrations.dnd_mcp.streamable_http_client",
        fake_transport,
    )
    monkeypatch.setattr(
        "sagasmith_service.integrations.dnd_mcp.ClientSession",
        lambda *_args: FakeSession(),
    )
    base_labels = {
        "system": "dnd5e",
        "operation_class": "request",
        "status": "success",
        "transport": "streamable_http",
    }
    metric_names = [
        "sagasmith_mcp_transport_setup_seconds_count",
        "sagasmith_mcp_initialize_seconds_count",
        "sagasmith_mcp_exposure_seconds_count",
        "sagasmith_mcp_tool_seconds_count",
    ]
    before = {name: _sample(name, base_labels) for name in metric_names}

    async def exercise() -> None:
        client = httpx.AsyncClient()
        runtime = StreamableHttpDndRuntime("http://dnd.test/mcp", http_client=client)
        first = await runtime.get_campaign(campaign_id="campaign-1", principal_id="user:1")
        second = await runtime.get_campaign(campaign_id="campaign-1", principal_id="user:1")
        assert first == second == {"result": {"id": "campaign-1"}}
        await runtime.aclose()
        assert not client.is_closed
        await client.aclose()

    asyncio.run(exercise())

    assert len(sessions) == 2
    assert len(seen_clients) == 2
    assert seen_clients[0] is seen_clients[1]
    for name in metric_names:
        assert _sample(name, base_labels) == before[name] + 2


def test_room_projection_metrics_record_batch_size() -> None:
    labels = {
        "system": "dnd5e",
        "operation_class": "resolution",
        "status": "success",
        "transport": "streamable_http",
    }
    count_before = _sample("sagasmith_room_projection_batch_seconds_count", labels)
    jobs_before = _sample("sagasmith_room_projection_jobs_sum", labels)

    async def exercise() -> None:
        async def worker(value: int) -> int:
            return value * 2

        assert await _observed_projection_batch(
            [1, 2, 3],
            worker,
            system="dnd5e",
            operation_class="resolution",
        ) == [2, 4, 6]

    asyncio.run(exercise())

    assert _sample("sagasmith_room_projection_batch_seconds_count", labels) == count_before + 1
    assert _sample("sagasmith_room_projection_jobs_sum", labels) == jobs_before + 3


def test_upstream_metric_labels_are_bounded() -> None:
    expected = ("system", "operation_class", "status", "transport")
    metrics = (
        AGENT_UPSTREAM_SECONDS,
        MCP_TRANSPORT_SETUP_SECONDS,
        MCP_INITIALIZE_SECONDS,
        MCP_EXPOSURE_SECONDS,
        MCP_TOOL_SECONDS,
        ROOM_PROJECTION_BATCH_SECONDS,
        ROOM_PROJECTION_JOBS,
    )
    assert all(metric._labelnames == expected for metric in metrics)
