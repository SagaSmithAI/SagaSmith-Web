import asyncio
import json
import socket
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sagasmith_service.agent_supervisor import (
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

    async def execute_narrative(
        self, operation: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
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
    assert any(getattr(timeout, "read", None) == 777 for timeout in client_timeouts)


def test_worker_runtime_config_uses_exact_trusted_host_cidrs(monkeypatch, tmp_path: Path) -> None:
    def fake_getaddrinfo(host: str, *_args):
        assert host == "dnd-mcp"
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("172.30.0.8", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert trusted_host_cidrs("dnd-mcp,dnd-mcp") == ["172.30.0.8/32"]

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"tools": {"ssrfWhitelist": ["127.0.0.1/32"]}}))
    assert json.loads(config.read_text())["tools"]["ssrfWhitelist"] == ["127.0.0.1/32"]
