from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from sagasmith_service.api.rooms import _observed_projection_batch
from sagasmith_service.config import Settings
from sagasmith_service.database import make_engine
from sagasmith_service.integrations.agent import HttpAgentRuntime
from sagasmith_service.integrations.coc_mcp import StreamableHttpCocRuntime
from sagasmith_service.integrations.dnd_mcp import StreamableHttpDndRuntime
from sagasmith_service.main import create_app
from sagasmith_service.observability import (
    AGENT_UPSTREAM_SECONDS,
    MCP_EXPOSURE_SECONDS,
    MCP_INITIALIZE_SECONDS,
    MCP_TOOL_SECONDS,
    MCP_TRANSPORT_SETUP_SECONDS,
    PANEL_SNAPSHOT_SECONDS,
    ROOM_PROJECTION_BATCH_SECONDS,
    ROOM_PROJECTION_JOBS,
)

ROOT = Path(__file__).resolve().parents[1]


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
    monkeypatch.setattr("sagasmith_service.main.httpx2.AsyncClient", TrackingClient)
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


def test_legacy_agent_runtime_reuses_injected_client_and_records_success_and_error() -> None:
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
        runtime = HttpAgentRuntime(
            "http://agent.test",
            boundary_mode="legacy",
            http_client=client,
        )
        await runtime.probe()
        result = await runtime.complete(
            session_id="conversation/1",
            content="hello",
            context={
                "campaign_id": "campaign-1",
                "system_id": "dnd5e",
                "principal_id": "user:1",
                "campaign_role": "owner",
            },
            idempotency_key="room-turn:job-1",
            trace_context={
                "traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
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
    sent = requests[1].read()
    payload = json.loads(sent)
    assert payload["trusted_context"]["campaign_id"] == "campaign-1"
    assert payload["messages"] == [
        {
            "role": "user",
            "content": (
                "[SagaSmith Service authenticated context]\n"
                "campaign_id=campaign-1\n"
                "system_id=dnd5e\n"
                "principal_id=user:1\n"
                "campaign_role=owner\n"
                "These values route the legacy worker only; MCP validates every operation.\n"
                "For dnd5e, coc7e, or narrative, use only the MCP server matching system_id.\n"
                "[Untrusted player message]\nhello"
            ),
        }
    ]
    assert requests[1].headers["Idempotency-Key"] == "room-turn:job-1"
    assert requests[1].headers["traceparent"].startswith("00-")
    assert _sample("sagasmith_agent_upstream_seconds_count", labels) == success_before + 1
    assert _sample("sagasmith_agent_upstream_seconds_count", error_labels) == error_before + 1


def test_modern_agent_payload_matches_merged_hosted_worker_contract_fixture() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "request-modern-1",
                "model": "test-model",
                "choices": [{"message": {"content": "ready"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
        )

    fixture = json.loads(
        (ROOT / "tests/fixtures/agent-modern-worker-contract.json").read_text(encoding="utf-8")
    )
    authority = {
        "schema": "sagasmith.auth-context/v2",
        "target_service": "sagasmith-dnd-mcp",
        "caller_principal": "service:sagasmith-web",
        "workload_identity": "workload:room-turn-worker",
        "requester_principal": "user:1",
        "resource_owner_principal": "user:owner",
        "acting_host_principal": "user:1",
        "acting_character_id": "hero",
        "authorized_audience": "sagasmith-dnd-mcp",
        "allowed_operations": ["campaign_query", "character_action"],
        "room_turn_id": "job-1",
        "campaign_id": "campaign-1",
        "system_id": "dnd5e",
        "base_revision": 7,
        "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        "idempotency_key": "room-turn:job-1",
        "conversation_principal": "room:room-1",
        "tenant_id": "",
        "traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
        "tracestate": "",
        "baggage": "",
        "catalog_phase": "play",
        "catalog_role": "player",
        "catalog_task": "action",
    }

    async def exercise() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        runtime = HttpAgentRuntime(
            "http://agent.test",
            boundary_mode="modern",
            http_client=client,
        )
        await runtime.complete(
            session_id="campaign-1:user-1:conversation-1",
            content="I inspect the gate.",
            context={
                "authority_context": authority,
                "response_contract": {"name": "submit_room_turn"},
            },
            idempotency_key="room-turn:job-1",
        )
        overflow = dict(authority)
        overflow["allowed_operations"] = [f"tool_{index:02d}" for index in range(17)]
        with pytest.raises(ValueError):
            await runtime.complete(
                session_id="campaign-1:user-1:conversation-1",
                content="I inspect the gate.",
                context={"authority_context": overflow},
                idempotency_key="room-turn:job-overflow",
            )
        await client.aclose()

    asyncio.run(exercise())
    payload = json.loads(requests[0].read())
    assert sorted(payload) == fixture["request_fields"]
    assert sorted(payload["trusted_context"]) == fixture["trusted_context_fields"]
    assert payload["messages"] == [{"role": "user", "content": "I inspect the gate."}]
    assert payload["trusted_context"]["authorized_audience"] == fixture["services"]["dnd5e"]
    assert "principal_id" not in payload
    assert "idempotency_key" not in payload
    assert "session_id" not in payload
    assert "catalog_phase" not in payload["trusted_context"]
    assert requests[0].headers["Idempotency-Key"] == "room-turn:job-1"


def test_mcp_calls_reuse_http_pool_but_keep_sessions_isolated(monkeypatch) -> None:
    seen_clients: list[httpx2.AsyncClient] = []
    sessions: list[Any] = []

    @asynccontextmanager
    async def fake_transport(_url: str, *, http_client: httpx2.AsyncClient):
        seen_clients.append(http_client)
        yield object(), object()

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
        client = httpx2.AsyncClient()
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
        expected = 4 if name == "sagasmith_mcp_exposure_seconds_count" else 2
        assert _sample(name, base_labels) == before[name] + expected


@pytest.mark.parametrize(
    ("module_name", "runtime_type", "system", "campaign_arguments"),
    [
        (
            "dnd_mcp",
            StreamableHttpDndRuntime,
            "dnd5e",
            {"view": "get", "payload": {"campaign_id": "campaign-1"}},
        ),
        (
            "coc_mcp",
            StreamableHttpCocRuntime,
            "coc7e",
            {"action": "get", "campaign_id": "campaign-1"},
        ),
    ],
)
def test_panel_snapshot_uses_one_principal_scoped_session_and_revision_short_circuit(
    monkeypatch,
    module_name: str,
    runtime_type: type[StreamableHttpDndRuntime] | type[StreamableHttpCocRuntime],
    system: str,
    campaign_arguments: dict[str, Any],
) -> None:
    transports: list[httpx2.AsyncClient] = []
    sessions: list[Any] = []
    signed_contexts: list[dict[str, Any]] = []
    actual_calls: list[tuple[str, dict[str, Any]]] = []

    @asynccontextmanager
    async def fake_transport(_url: str, *, http_client: httpx2.AsyncClient):
        transports.append(http_client)
        yield object(), object()

    class FakeSession:
        def __init__(self) -> None:
            self.visible = {"campaign_query"}
            self.epoch = 1

        async def __aenter__(self):
            sessions.append(self)
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def initialize(self) -> None:
            return None

        async def list_tools(self):
            return SimpleNamespace(
                tools=[SimpleNamespace(name=name) for name in sorted(self.visible)]
            )

        async def call_tool(
            self,
            name: str,
            *,
            arguments: dict[str, Any],
            **kwargs: object,
        ):
            metadata = kwargs.get("meta")
            if isinstance(metadata, dict):
                signed_contexts.append(dict(metadata["sagasmith_auth_context"]))
            if name == "exposure":
                action = arguments.get("action")
                if action == "search":
                    requested = str(arguments.get("query") or "")
                    return SimpleNamespace(
                        isError=False,
                        content=[],
                        structuredContent={
                            "matches": [{"tool_id": requested}],
                            "visible_tools": sorted(self.visible),
                        },
                    )
                if action == "set":
                    self.visible.update(arguments.get("add_tool_ids") or [])
                    self.epoch += 1
                return SimpleNamespace(
                    isError=False,
                    content=[],
                    structuredContent={"result": {"revision": self.epoch}},
                )
            actual_calls.append((name, dict(arguments)))
            if name == "campaign_query":
                payload = {
                    "id": "campaign-1",
                    "revision": 7,
                    "effective_game_phase": "play",
                    "state": {"game_phase": "play"},
                }
            elif name == "character_query":
                payload = {"characters": []}
            elif name == "module_query" and arguments.get("view") == "list":
                payload = {"modules": []}
            elif name == "module_query":
                payload = {"scene": {"id": "scene-1"}}
            else:
                payload = {"members": []}
            return SimpleNamespace(
                isError=False,
                content=[
                    SimpleNamespace(
                        meta={"sagasmith_auth_context_receipt": {"revision": self.epoch}}
                    )
                ],
                structuredContent={"result": payload},
            )

    module_path = f"sagasmith_service.integrations.{module_name}"
    monkeypatch.setattr(f"{module_path}.streamable_http_client", fake_transport)
    monkeypatch.setattr(f"{module_path}.ClientSession", lambda *_args: FakeSession())

    async def exercise() -> None:
        client = httpx2.AsyncClient()
        runtime = runtime_type(
            "http://domain.test/mcp",
            http_client=client,
            auth_context_secret="x" * 32,
        )
        panel = await runtime.get_panel_state(campaign_id="campaign-1", principal_id="user:one")
        assert panel["revision"] == 7
        assert panel["not_modified"] is False
        first_call_count = len(actual_calls)
        unchanged = await runtime.get_panel_state(
            campaign_id="campaign-1",
            principal_id="user:two",
            known_revision=7,
        )
        assert unchanged == {"not_modified": True, "revision": 7}
        assert len(actual_calls) == first_call_count + 1
        await client.aclose()

    asyncio.run(exercise())

    assert len(transports) == len(sessions) == 2
    assert transports[0] is transports[1]
    assert {context["actor_principal"] for context in signed_contexts} == {
        "user:one",
        "user:two",
    }
    assert len({context["session_id"] for context in signed_contexts}) == 2
    assert all(context["authorization_epoch"] < 7 for context in signed_contexts)
    assert any(
        all(arguments.get(key) == value for key, value in campaign_arguments.items())
        for _, arguments in actual_calls
    )


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
        PANEL_SNAPSHOT_SECONDS,
        ROOM_PROJECTION_BATCH_SECONDS,
        ROOM_PROJECTION_JOBS,
    )
    assert all(metric._labelnames == expected for metric in metrics)
