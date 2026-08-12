import asyncio
import json
import socket
from pathlib import Path
from typing import Any

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


def test_supervisor_authenticates_and_routes_by_conversation() -> None:
    manager = FakeManager()
    app = create_supervisor_app(manager, "internal-secret")  # type: ignore[arg-type]
    with TestClient(app) as client:
        assert manager.started is True
        assert client.get("/health").status_code == 200
        assert client.post(
            "/v1/conversations/campaign:test-user:conversation/completions",
            json={"messages": [], "principal_id": "user:test-user"},
        ).status_code == 401
        response = client.post(
            "/v1/conversations/campaign:test-user:conversation/completions",
            headers={"Authorization": "Bearer internal-secret"},
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "principal_id": "user:test-user",
            },
        )
        assert response.status_code == 200
        assert manager.calls[0][0] == "campaign:test-user:conversation"
        assert manager.calls[0][1]["stream"] is False
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
    assert conversation_matches_principal(
        "campaign-id:user-id:conversation-id", "user:user-id"
    )
    assert not conversation_matches_principal("campaign-id:conversation-id", "user:user-id")
    assert not conversation_matches_principal(
        "campaign-id:user-id:conversation-id", "user:other-id"
    )


def test_worker_manager_isolates_and_reuses_conversation_processes(
    monkeypatch, tmp_path: Path
) -> None:
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
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def get(self, _url: str) -> FakeResponse:
            return FakeResponse()

        async def post(self, _url: str, json: dict[str, Any]) -> FakeResponse:
            assert json["stream"] is False
            assert "model" not in json
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
        assert manager.workers["campaign-a:user-a:conversation-a"].port != manager.workers[
            "campaign-b:user-b:conversation-b"
        ].port
        await manager.close()

    asyncio.run(scenario())
    assert all(process.terminated for process in processes)


def test_worker_runtime_config_uses_exact_trusted_host_cidrs(monkeypatch, tmp_path: Path) -> None:
    def fake_getaddrinfo(host: str, *_args):
        assert host == "dnd-mcp"
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("172.30.0.8", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert trusted_host_cidrs("dnd-mcp,dnd-mcp") == ["172.30.0.8/32"]

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"tools": {"ssrfWhitelist": ["127.0.0.1/32"]}}))
    assert json.loads(config.read_text())["tools"]["ssrfWhitelist"] == ["127.0.0.1/32"]
