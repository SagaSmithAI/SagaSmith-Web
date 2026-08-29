import asyncio
import json
import socket
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sagasmith_service.agent_supervisor import (
    HOSTED_WORKSPACE_ADMISSION_LOCK,
    WORKSPACE_MARKER,
    Worker,
    WorkerCapacityError,
    WorkerManager,
    conversation_matches_principal,
    create_supervisor_app,
    trusted_host_cidrs,
)


class FakeManager:
    def __init__(self) -> None:
        self.started = False
        self.closed = False
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.narrative_calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def complete(self, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("worker failed")
        self.calls.append((key, payload))
        return {"id": "completion-1", "choices": []}

    async def probe_narrative(self) -> None:
        if self.fail:
            raise RuntimeError("narrative failed")

    async def execute_narrative(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("narrative failed")
        self.narrative_calls.append((operation, arguments))
        return {"id": "narrative-campaign"}

    def worker_status(self) -> dict[str, Any]:
        return {
            "active_workers": 1,
            "tracked_pids": [101],
            "orphan_pids": [],
        }


class FakeNarrativeControl:
    def __init__(self) -> None:
        self.probes = 0
        self.fail = False

    async def probe(self) -> None:
        self.probes += 1
        if self.fail:
            raise RuntimeError("narrative unavailable")


def test_worker_manager_uses_dedicated_service_token_for_child_requests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured_headers: list[dict[str, str]] = []

    class CapturingClient:
        def __init__(self, *, headers: dict[str, str], **_kwargs: Any) -> None:
            captured_headers.append(headers)

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr("sagasmith_service.agent_supervisor.httpx.AsyncClient", CapturingClient)
    WorkerManager(
        config_path=str(tmp_path / "config.json"),
        workspace_root=str(tmp_path / "workspaces"),
        worker_service_token="dedicated-worker-service-token-at-least-32-bytes",
    )
    assert captured_headers == [
        {"Authorization": "Bearer dedicated-worker-service-token-at-least-32-bytes"},
        {"Authorization": "Bearer dedicated-worker-service-token-at-least-32-bytes"},
    ]


def test_worker_manager_coalesces_successful_narrative_readiness_probes(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        narrative = FakeNarrativeControl()
        manager = WorkerManager(
            config_path=str(tmp_path / "config.json"),
            workspace_root=str(tmp_path / "workspaces"),
            worker_api_key="secret",
            narrative_control=narrative,  # type: ignore[arg-type]
        )

        await asyncio.gather(*(manager.probe_narrative() for _ in range(5)))
        await manager.probe_narrative()

        assert narrative.probes == 1
        narrative.fail = True
        manager._narrative_probe_success_at = 0
        with pytest.raises(RuntimeError, match="unavailable"):
            await manager.probe_narrative()

    asyncio.run(scenario())


def test_supervisor_authenticates_and_routes_by_conversation() -> None:
    manager = FakeManager()
    app = create_supervisor_app(manager, "internal-secret")  # type: ignore[arg-type]
    with TestClient(app) as client:
        assert manager.started is True
        assert client.get("/health").status_code == 200
        assert (
            client.post(
                "/v1/conversations/campaign:test-user:conversation/completions",
                json={"messages": [], "principal_id": "user:test-user"},
            ).status_code
            == 401
        )
        response = client.post(
            "/v1/conversations/campaign:test-user:conversation/completions",
            headers={"Authorization": "Bearer internal-secret"},
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "principal_id": "user:test-user",
                "response_contract": {
                    "name": "submit_result",
                    "description": "Submit.",
                    "parameters": {"type": "object"},
                },
            },
        )
        assert response.status_code == 200
        assert manager.calls[0][0] == "campaign:test-user:conversation"
        assert manager.calls[0][1]["stream"] is False
        assert manager.calls[0][1]["response_contract"]["name"] == "submit_result"
    assert manager.closed is True


def test_supervisor_forwards_modern_context_without_legacy_extra_fields() -> None:
    manager = FakeManager()
    trusted_context = {
        "caller_principal": "service:sagasmith-web",
        "workload_identity": "workload:room-turn-worker",
        "requester_principal": "user:test-user",
        "resource_owner_principal": "user:owner",
        "acting_host_principal": "user:test-user",
        "acting_character_id": "",
        "authorized_audience": "sagasmith-dnd-mcp",
        "allowed_operations": ["campaign_query"],
        "room_turn_id": "turn-1",
        "campaign_id": "campaign",
        "system_id": "dnd5e",
        "base_revision": 0,
        "expires_at": "2026-08-29T06:00:00Z",
        "idempotency_key": "room-turn:turn-1",
        "conversation_principal": "room:one",
        "tenant_id": "",
        "traceparent": "",
        "tracestate": "",
        "baggage": "",
    }
    with TestClient(
        create_supervisor_app(manager, "internal-secret")  # type: ignore[arg-type]
    ) as client:
        response = client.post(
            "/v1/conversations/campaign:test-user:conversation/completions",
            headers={"Authorization": "Bearer internal-secret"},
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "trusted_context": trusted_context,
                "stream": False,
                "response_contract": None,
                "terminal": False,
            },
        )
    assert response.status_code == 200, response.text
    forwarded = manager.calls[0][1]
    assert forwarded["trusted_context"] == trusted_context
    assert "session_id" not in forwarded
    assert "principal_id" not in forwarded
    assert "idempotency_key" not in forwarded


def test_supervisor_accepts_identity_conversation_only_from_acting_host() -> None:
    manager = FakeManager()
    with TestClient(
        create_supervisor_app(manager, "internal-secret")  # type: ignore[arg-type]
    ) as client:
        response = client.post(
            "/v1/conversations/campaign:agent:identity-1:conversation/completions",
            headers={"Authorization": "Bearer internal-secret"},
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "trusted_context": {
                    "requester_principal": "user:test-user",
                    "acting_host_principal": "agent:identity-1",
                },
            },
        )
    assert response.status_code == 200, response.text


def test_supervisor_maps_worker_failure() -> None:
    manager = FakeManager()
    manager.fail = True
    with TestClient(
        create_supervisor_app(manager, "internal-secret")  # type: ignore[arg-type]
    ) as client:
        response = client.post(
            "/v1/conversations/campaign:test-user:conversation/completions",
            headers={"Authorization": "Bearer internal-secret"},
            json={"messages": [], "principal_id": "user:test-user"},
        )
    assert response.status_code == 502


def test_supervisor_maps_worker_capacity_to_retryable_unavailability() -> None:
    class CapacityManager(FakeManager):
        async def complete(self, key: str, payload: dict[str, Any]) -> dict[str, Any]:
            raise WorkerCapacityError("Agent worker capacity is full")

    manager = CapacityManager()
    with TestClient(
        create_supervisor_app(manager, "internal-secret")  # type: ignore[arg-type]
    ) as client:
        response = client.post(
            "/v1/conversations/campaign:test-user:conversation/completions",
            headers={"Authorization": "Bearer internal-secret"},
            json={"messages": [], "principal_id": "user:test-user"},
        )
    assert response.status_code == 503


def test_supervisor_authenticates_narrative_health_and_fixed_operations() -> None:
    manager = FakeManager()
    with TestClient(
        create_supervisor_app(manager, "internal-secret")  # type: ignore[arg-type]
    ) as client:
        assert client.get("/health/narrative").status_code == 401
        assert (
            client.get(
                "/health/narrative",
                headers={"Authorization": "Bearer internal-secret"},
            ).status_code
            == 200
        )
        response = client.post(
            "/v1/narrative/operations",
            headers={"Authorization": "Bearer internal-secret"},
            json={
                "operation": "get_campaign",
                "arguments": {
                    "campaign_id": "campaign-1",
                    "principal_id": "user:one",
                },
            },
        )
    assert response.status_code == 200
    assert response.json()["id"] == "narrative-campaign"
    assert manager.narrative_calls == [
        (
            "get_campaign",
            {"campaign_id": "campaign-1", "principal_id": "user:one"},
        )
    ]


def test_supervisor_authenticates_and_reports_worker_lifecycle() -> None:
    manager = FakeManager()
    with TestClient(
        create_supervisor_app(manager, "internal-secret")  # type: ignore[arg-type]
    ) as client:
        assert client.get("/health/workers").status_code == 401
        response = client.get(
            "/health/workers",
            headers={"Authorization": "Bearer internal-secret"},
        )
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "active_workers": 1,
        "tracked_pids": [101],
        "orphan_pids": [],
    }


def test_supervisor_rejects_principal_conversation_mismatch() -> None:
    manager = FakeManager()
    with TestClient(
        create_supervisor_app(manager, "internal-secret")  # type: ignore[arg-type]
    ) as client:
        response = client.post(
            "/v1/conversations/campaign:other-user:conversation/completions",
            headers={"Authorization": "Bearer internal-secret"},
            json={"messages": [], "principal_id": "user:test-user"},
        )
    assert response.status_code == 403
    assert manager.calls == []


def test_conversation_principal_binding_requires_canonical_key() -> None:
    assert conversation_matches_principal("campaign-id:user-id:conversation-id", "user:user-id")
    assert conversation_matches_principal(
        "campaign-id:agent:identity-id:conversation-id", "agent:identity-id"
    )
    assert not conversation_matches_principal("campaign-id:conversation-id", "user:user-id")
    assert not conversation_matches_principal(
        "campaign-id:user-id:conversation-id", "user:other-id"
    )
    assert not conversation_matches_principal(
        "campaign-id:agent:identity-id:conversation-id", "agent:other-id"
    )


def test_worker_manager_isolates_and_reuses_conversation_processes(
    monkeypatch, tmp_path: Path
) -> None:
    client_timeouts: list[Any] = []

    class FakeProcess:
        def __init__(self) -> None:
            self.returncode = None
            self.terminated = False

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int:
            return self.returncode or 0

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {"id": "completion", "choices": []}

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            client_timeouts.append(kwargs.get("timeout"))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def aclose(self) -> None:
            return None

        async def get(self, _url: str) -> FakeResponse:
            return FakeResponse()

        async def post(self, _url: str, json: dict[str, Any]) -> FakeResponse:
            assert json["stream"] is False
            assert "model" not in json
            assert sum(worker.active_requests for worker in manager.workers.values()) == 1
            return FakeResponse()

    processes: list[FakeProcess] = []

    async def fake_spawn(*_args: Any) -> FakeProcess:
        process = FakeProcess()
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr("sagasmith_service.agent_supervisor.httpx.AsyncClient", FakeClient)
    manager = WorkerManager(
        config_path=str(tmp_path / "config.json"),
        workspace_root=str(tmp_path / "workspaces"),
        worker_api_key="secret",
        idle_seconds=3600,
        completion_timeout_seconds=777,
    )
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    async def scenario() -> None:
        await manager.start()
        first = await manager.complete(
            "campaign-a:user-a:conversation-a",
            {"messages": [], "model": "untrusted-placeholder"},
        )
        repeated = await manager.complete("campaign-a:user-a:conversation-a", {"messages": []})
        second = await manager.complete("campaign-b:user-b:conversation-b", {"messages": []})
        assert first == repeated == second
        assert len(processes) == 2
        assert len(manager.workers) == 2
        assert all(worker.active_requests == 0 for worker in manager.workers.values())
        assert (
            manager.workers["campaign-a:user-a:conversation-a"].port
            != manager.workers["campaign-b:user-b:conversation-b"].port
        )
        await manager.close()

    asyncio.run(scenario())
    assert all(process.terminated for process in processes)
    assert len(client_timeouts) == 2
    assert any(getattr(timeout, "read", None) == 777 for timeout in client_timeouts)


def test_worker_manager_does_not_reuse_port_until_idle_worker_exits(
    monkeypatch, tmp_path: Path
) -> None:
    stop_started = asyncio.Event()
    allow_stop = asyncio.Event()

    class FakeProcess:
        def __init__(self) -> None:
            self.returncode = None

        def terminate(self) -> None:
            stop_started.set()

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int:
            await allow_stop.wait()
            self.returncode = 0
            return 0

    class FakeResponse:
        status_code = 200

    class FakeClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def aclose(self) -> None:
            return None

        async def get(self, _url: str) -> FakeResponse:
            return FakeResponse()

    async def fake_spawn(*_args: Any) -> FakeProcess:
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr("sagasmith_service.agent_supervisor.httpx.AsyncClient", FakeClient)
    manager = WorkerManager(
        config_path=str(tmp_path / "config.json"),
        workspace_root=str(tmp_path / "workspaces"),
        worker_api_key="secret",
        idle_seconds=0,
    )
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")

    async def scenario() -> None:
        old_worker = await manager.get("campaign-a:user-a:conversation-a")
        old_worker.last_used = -1
        cleanup = asyncio.create_task(manager._retire_expired_workers())
        await stop_started.wait()

        new_worker = await manager.get("campaign-b:user-b:conversation-b")
        assert old_worker.port == 19000
        assert new_worker.port == 19001

        allow_stop.set()
        await cleanup
        await manager.close()

    asyncio.run(scenario())


def test_worker_restart_reuses_host_workspace_id_across_ports(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeProcess:
        returncode: int | None = None
        pid: int | None = None

        def terminate(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int:
            return self.returncode or 0

    class FakeResponse:
        status_code = 200

    class FakeClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def get(self, _url: str) -> FakeResponse:
            return FakeResponse()

        async def aclose(self) -> None:
            return None

    commands: list[tuple[Any, ...]] = []

    async def fake_spawn(*arguments: Any) -> FakeProcess:
        commands.append(arguments)
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr("sagasmith_service.agent_supervisor.httpx.AsyncClient", FakeClient)
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    manager = WorkerManager(
        config_path=str(config),
        workspace_root=str(tmp_path / "workspaces"),
        worker_api_key="secret",
    )
    key = "campaign-a:user-a:conversation-a"
    other_key = "campaign-b:user-b:conversation-b"

    def option(command: tuple[Any, ...], name: str) -> str:
        index = command.index(name)
        return str(command[index + 1])

    async def scenario() -> None:
        first = await manager._spawn(key, 19000)
        await manager._stop(first)
        restarted = await manager._spawn(key, 19001)
        await manager._stop(restarted)
        other = await manager._spawn(other_key, 19002)
        await manager._stop(other)
        await manager.close()

    asyncio.run(scenario())

    assert option(commands[0], "--port") != option(commands[1], "--port")
    assert option(commands[0], "--workspace-id") == option(commands[1], "--workspace-id")
    assert option(commands[0], "--workspace-id") == manager._workspace(key).name
    assert option(commands[2], "--workspace-id") == manager._workspace(other_key).name
    assert option(commands[2], "--workspace-id") != option(commands[0], "--workspace-id")


def test_worker_runtime_config_uses_exact_trusted_host_cidrs(monkeypatch, tmp_path: Path) -> None:
    def fake_getaddrinfo(host: str, *_args):
        assert host == "dnd-mcp"
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("172.30.0.8", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert trusted_host_cidrs("dnd-mcp,dnd-mcp") == ["172.30.0.8/32"]

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"tools": {"ssrfWhitelist": ["127.0.0.1/32"]}}))
    assert json.loads(config.read_text())["tools"]["ssrfWhitelist"] == ["127.0.0.1/32"]


def test_worker_manager_singleflights_keys_and_bounds_parallel_spawns(tmp_path: Path) -> None:
    class FakeProcess:
        returncode: int | None = None
        pid: int | None = None

        def terminate(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int:
            return self.returncode or 0

    manager = WorkerManager(
        config_path=str(tmp_path / "config.json"),
        workspace_root=str(tmp_path / "workspaces"),
        worker_api_key="secret",
        max_workers=4,
        spawn_concurrency=2,
    )
    release_spawns = asyncio.Event()
    two_started = asyncio.Event()
    calls: list[str] = []
    active_spawns = 0
    maximum_active_spawns = 0

    async def fake_spawn(key: str, port: int) -> Worker:
        nonlocal active_spawns, maximum_active_spawns
        calls.append(key)
        active_spawns += 1
        maximum_active_spawns = max(maximum_active_spawns, active_spawns)
        if active_spawns == 2:
            two_started.set()
        await release_spawns.wait()
        active_spawns -= 1
        return Worker(
            key=key,
            port=port,
            process=FakeProcess(),  # type: ignore[arg-type]
            last_used=0,
            runtime_config_path=tmp_path / f"{port}.json",
        )

    manager._spawn = fake_spawn  # type: ignore[method-assign]

    async def scenario() -> None:
        tasks = [
            asyncio.create_task(manager.get("campaign-a:user-a:conversation-a")),
            asyncio.create_task(manager.get("campaign-a:user-a:conversation-a")),
            asyncio.create_task(manager.get("campaign-b:user-b:conversation-b")),
            asyncio.create_task(manager.get("campaign-c:user-c:conversation-c")),
        ]
        await asyncio.wait_for(two_started.wait(), timeout=1)
        assert maximum_active_spawns == 2
        release_spawns.set()
        first, repeated, *_others = await asyncio.gather(*tasks)
        assert first is repeated
        assert calls.count("campaign-a:user-a:conversation-a") == 1
        assert len(calls) == 3
        await manager.close()

    asyncio.run(scenario())


def test_worker_manager_rejects_busy_capacity_then_evicts_lru(tmp_path: Path) -> None:
    class FakeProcess:
        returncode: int | None = None
        pid: int | None = None

        def __init__(self) -> None:
            self.terminated = False

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int:
            return self.returncode or 0

    manager = WorkerManager(
        config_path=str(tmp_path / "config.json"),
        workspace_root=str(tmp_path / "workspaces"),
        worker_api_key="secret",
        max_workers=1,
    )
    processes: list[FakeProcess] = []

    async def fake_spawn(key: str, port: int) -> Worker:
        process = FakeProcess()
        processes.append(process)
        return Worker(
            key=key,
            port=port,
            process=process,  # type: ignore[arg-type]
            last_used=0,
            runtime_config_path=tmp_path / f"{port}.json",
        )

    manager._spawn = fake_spawn  # type: ignore[method-assign]

    async def scenario() -> None:
        first = await manager.get("campaign-a:user-a:conversation-a")
        first.active_requests = 1
        with pytest.raises(WorkerCapacityError, match="capacity is full"):
            await manager.get("campaign-b:user-b:conversation-b")
        first.active_requests = 0
        second = await manager.get("campaign-b:user-b:conversation-b")
        assert second.key == "campaign-b:user-b:conversation-b"
        assert first.process.terminated is True
        assert list(manager.workers) == ["campaign-b:user-b:conversation-b"]
        await manager.close()

    asyncio.run(scenario())


def test_supervisor_exposes_worker_metrics() -> None:
    manager = FakeManager()
    with TestClient(
        create_supervisor_app(manager, "internal-secret")  # type: ignore[arg-type]
    ) as client:
        response = client.get("/metrics")
    assert response.status_code == 200
    assert "sagasmith_agent_workers" in response.text
    assert "sagasmith_agent_worker_rss_bytes" in response.text
    assert "sagasmith_agent_workspace_bytes" in response.text
    assert "sagasmith_agent_workspace_cleanup_total" in response.text


def _register_test_workspace(
    manager: WorkerManager,
    key: str,
    *,
    state: str = "active",
    last_used_at: float | None = None,
) -> Path:
    manager._initialize_workspace_store()
    workspace = manager._workspace(key)
    workspace.mkdir(mode=0o700)
    timestamp = last_used_at or time.time()
    manager._write_workspace_marker(
        workspace,
        state=state,
        created_at=min(timestamp, time.time()),
        last_used_at=timestamp,
    )
    return workspace


def test_workspace_startup_recovers_recent_crash_and_expires_stale_active(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspaces"
    manager = WorkerManager(
        config_path=str(tmp_path / "config.json"),
        workspace_root=str(workspace_root),
        worker_api_key="secret",
        workspace_ttl_seconds=60,
    )
    recent = _register_test_workspace(
        manager,
        "campaign-a:user-a:recent",
        last_used_at=time.time(),
    )
    stale = _register_test_workspace(
        manager,
        "campaign-a:user-a:stale",
        last_used_at=time.time() - 120,
    )

    restarted = WorkerManager(
        config_path=str(tmp_path / "config.json"),
        workspace_root=str(workspace_root),
        worker_api_key="secret",
        workspace_ttl_seconds=60,
    )
    restarted._initialize_workspace_store()

    assert recent.is_dir()
    assert json.loads((recent / WORKSPACE_MARKER).read_text())["state"] == "idle"
    assert not stale.exists()
    assert restarted._workspace_snapshot.records[0].workspace_id == recent.name


def test_workspace_startup_enforces_lru_count_for_crash_leftovers(tmp_path: Path) -> None:
    manager = WorkerManager(
        config_path=str(tmp_path / "config.json"),
        workspace_root=str(tmp_path / "workspaces"),
        worker_api_key="secret",
        workspace_ttl_seconds=3600,
        max_workspaces=2,
    )
    oldest = _register_test_workspace(
        manager,
        "campaign-a:user-a:oldest",
        last_used_at=time.time() - 30,
    )
    middle = _register_test_workspace(
        manager,
        "campaign-a:user-a:middle",
        last_used_at=time.time() - 20,
    )
    newest = _register_test_workspace(
        manager,
        "campaign-a:user-a:newest",
        last_used_at=time.time() - 10,
    )

    restarted = WorkerManager(
        config_path=str(tmp_path / "config.json"),
        workspace_root=str(tmp_path / "workspaces"),
        worker_api_key="secret",
        workspace_ttl_seconds=3600,
        max_workspaces=2,
    )
    restarted._initialize_workspace_store()

    assert not oldest.exists()
    assert middle.is_dir()
    assert newest.is_dir()
    assert len(restarted._workspace_snapshot.records) == 2


def test_workspace_byte_capacity_rejects_when_only_workspace_is_protected(
    tmp_path: Path,
) -> None:
    manager = WorkerManager(
        config_path=str(tmp_path / "config.json"),
        workspace_root=str(tmp_path / "workspaces"),
        worker_api_key="secret",
        workspace_max_bytes=32,
    )
    key = "campaign-a:user-a:oversized"
    workspace = _register_test_workspace(manager, key, state="idle")
    (workspace / "payload.bin").write_bytes(b"x" * 128)

    with pytest.raises(WorkerCapacityError, match="workspace capacity is full"):
        asyncio.run(manager._prepare_workspace(key))
    assert workspace.is_dir()


def test_workspace_touch_does_not_rescan_every_registered_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = WorkerManager(
        config_path=str(tmp_path / "config.json"),
        workspace_root=str(tmp_path / "workspaces"),
        worker_api_key="secret",
    )
    workspace = _register_test_workspace(manager, "campaign-a:user-a:hot-path")
    previous = manager._registered_workspace(workspace)
    assert previous is not None

    def fail_full_scan():
        raise AssertionError("request completion must not scan the full workspace namespace")

    monkeypatch.setattr(manager, "_scan_workspace_store", fail_full_scan)
    manager._touch_workspace(workspace)

    refreshed = next(
        item for item in manager._workspace_snapshot.records if item.workspace_id == workspace.name
    )
    assert refreshed.last_used_at >= previous.last_used_at


def test_new_workspace_rolls_back_when_registration_exceeds_byte_capacity(
    tmp_path: Path,
) -> None:
    manager = WorkerManager(
        config_path=str(tmp_path / "config.json"),
        workspace_root=str(tmp_path / "workspaces"),
        worker_api_key="secret",
        workspace_max_bytes=32,
    )
    key = "campaign-a:user-a:new-oversized"

    with pytest.raises(WorkerCapacityError, match="workspace capacity is full"):
        asyncio.run(manager._prepare_workspace(key))
    assert not manager._workspace(key).exists()


def test_workspace_cleanup_never_deletes_unknown_or_outside(
    tmp_path: Path,
) -> None:
    workspace_root = tmp_path / "workspaces"
    legacy = workspace_root / "legacy-conversation"
    legacy.mkdir(parents=True)
    (legacy / "keep.txt").write_text("keep", encoding="utf-8")
    manager = WorkerManager(
        config_path=str(tmp_path / "config.json"),
        workspace_root=str(workspace_root),
        worker_api_key="secret",
        workspace_ttl_seconds=1,
        max_workspaces=1,
    )
    manager._initialize_workspace_store()
    unknown = manager.managed_workspace_root / ("a" * 64)
    unknown.mkdir()
    (unknown / "keep.txt").write_text("keep", encoding="utf-8")
    (unknown / WORKSPACE_MARKER).write_text(
        json.dumps(
            {
                "schema": "sagasmith.agent-workspace/v1",
                "owner": "not-sagasmith",
                "workspace_id": unknown.name,
                "state": "idle",
                "created_at": time.time() - 100,
                "last_used_at": time.time() - 100,
            }
        ),
        encoding="utf-8",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(WorkerCapacityError, match="workspace capacity is full"):
        manager._prune_workspace_store(set(), 1)

    assert (legacy / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert (unknown / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert manager._delete_registered_workspace(outside, "test") is False
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert manager._workspace_snapshot.unknown_entries == 1
    assert manager._workspace_snapshot.unknown_bytes > 0


def test_workspace_scan_accepts_only_regular_agent_root_admission_lock(
    tmp_path: Path,
) -> None:
    manager = WorkerManager(
        config_path=str(tmp_path / "config.json"),
        workspace_root=str(tmp_path / "workspaces"),
        worker_api_key="secret",
    )
    manager._initialize_workspace_store()
    admission_lock = manager.managed_workspace_root / HOSTED_WORKSPACE_ADMISSION_LOCK
    admission_lock.touch()

    snapshot = manager._scan_workspace_store()

    assert snapshot.unknown_entries == 0
    assert snapshot.unknown_bytes == 0

    admission_lock.unlink()
    admission_lock.mkdir()
    (admission_lock / "keep.txt").write_text("keep", encoding="utf-8")

    snapshot = manager._scan_workspace_store()

    assert snapshot.unknown_entries == 1
    assert snapshot.unknown_bytes > 0
    manager._prune_workspace_store(set(), 0)
    assert (admission_lock / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_workspace_cleanup_never_deletes_top_level_symlink(tmp_path: Path) -> None:
    manager = WorkerManager(
        config_path=str(tmp_path / "config.json"),
        workspace_root=str(tmp_path / "workspaces"),
        worker_api_key="secret",
    )
    manager._initialize_workspace_store()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    link = manager.managed_workspace_root / ("b" * 64)
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are not available on this host")

    manager._prune_workspace_store(set(), 0)

    assert link.is_symlink()
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert manager._workspace_snapshot.unknown_entries == 1


def test_workspace_secure_delete_refuses_registered_tree_with_nested_symlink(
    tmp_path: Path,
) -> None:
    manager = WorkerManager(
        config_path=str(tmp_path / "config.json"),
        workspace_root=str(tmp_path / "workspaces"),
        worker_api_key="secret",
    )
    workspace = _register_test_workspace(
        manager,
        "campaign-a:user-a:linked",
        state="idle",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    nested_link = workspace / "outside-link"
    try:
        nested_link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are not available on this host")

    assert manager._delete_registered_workspace(workspace, "test") is False
    assert workspace.is_dir()
    assert nested_link.is_symlink()
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_terminal_completion_stops_worker_and_securely_deletes_registered_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeProcess:
        returncode: int | None = None
        pid: int | None = 123

        def __init__(self) -> None:
            self.terminated = False

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int:
            return self.returncode or 0

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, Any]:
            return {"id": "completion", "choices": []}

    class FakeClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def aclose(self) -> None:
            return None

        async def get(self, _url: str) -> FakeResponse:
            return FakeResponse()

        async def post(self, _url: str, json: dict[str, Any]) -> FakeResponse:
            return FakeResponse()

    process = FakeProcess()

    async def fake_spawn(*_args: Any) -> FakeProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr("sagasmith_service.agent_supervisor.httpx.AsyncClient", FakeClient)
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    manager = WorkerManager(
        config_path=str(config),
        workspace_root=str(tmp_path / "workspaces"),
        worker_api_key="secret",
    )
    key = "campaign-a:user-a:terminal"

    async def scenario() -> None:
        await manager.complete(key, {"messages": [], "terminal": False})
        (manager._workspace(key) / "oversized.bin").write_bytes(b"x" * 1024)
        manager._touch_workspace(manager._workspace(key))
        manager.workspace_max_bytes = 32
        result = await manager.complete(key, {"messages": [], "terminal": True})
        assert result["id"] == "completion"
        assert key not in manager.workers
        assert not manager._workspace(key).exists()
        await manager.close()

    asyncio.run(scenario())
    assert process.terminated is True
