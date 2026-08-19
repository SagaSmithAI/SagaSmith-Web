from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import socket
import tempfile
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel

from sagasmith_service.narrative_control import NarrativeControlClient, NarrativeOperation


class CompletionRequest(BaseModel):
    messages: list[dict[str, Any]]
    principal_id: str
    stream: bool = False
    response_contract: dict[str, Any] | None = None


class NarrativeOperationRequest(BaseModel):
    operation: NarrativeOperation
    arguments: dict[str, Any]


@dataclass
class Worker:
    key: str
    port: int
    process: asyncio.subprocess.Process
    last_used: float
    runtime_config_path: Path
    active_requests: int = 0


def trusted_host_cidrs(hosts: str) -> list[str]:
    """Resolve explicitly trusted internal MCP hostnames to exact host CIDRs."""
    networks: set[str] = set()
    for host in (item.strip() for item in hosts.split(",")):
        if not host:
            continue
        for info in socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM):
            address = ipaddress.ip_address(info[4][0])
            prefix = 32 if address.version == 4 else 128
            networks.add(f"{address}/{prefix}")
    return sorted(networks)


def conversation_matches_principal(conversation_key: str, principal_id: str) -> bool:
    """Bind the trusted service principal to its isolated conversation worker."""
    parts = conversation_key.split(":")
    if not all(parts):
        return False
    if len(parts) == 3:
        return principal_id == f"user:{parts[1]}"
    if len(parts) == 4 and parts[1] == "agent":
        return principal_id == f"agent:{parts[2]}"
    return False


class WorkerManager:
    """One Nanobot process and one real MCP session per conversation."""

    def __init__(
        self,
        *,
        config_path: str,
        workspace_root: str,
        worker_api_key: str,
        first_port: int = 19000,
        idle_seconds: int = 1800,
        completion_timeout_seconds: int = 900,
        narrative_control: NarrativeControlClient | None = None,
    ) -> None:
        self.config_path = str(Path(config_path).resolve())
        self.workspace_root = Path(workspace_root).resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.worker_api_key = worker_api_key
        self.first_port = first_port
        self.idle_seconds = idle_seconds
        self.completion_timeout_seconds = max(30, int(completion_timeout_seconds))
        self.narrative_control = narrative_control
        self.workers: dict[str, Worker] = {}
        self.retiring_ports: set[int] = set()
        self.lock = asyncio.Lock()
        self.cleanup_task: asyncio.Task[None] | None = None
        self._narrative_probe_lock = asyncio.Lock()
        self._narrative_probe_success_at = 0.0

    def _workspace(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode()).hexdigest()
        return self.workspace_root / digest

    async def start(self) -> None:
        self.cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def close(self) -> None:
        if self.cleanup_task is not None:
            self.cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.cleanup_task
        for worker in list(self.workers.values()):
            await self._stop(worker)
        self.workers.clear()

    async def _stop(self, worker: Worker) -> None:
        try:
            if worker.process.returncode is None:
                worker.process.terminate()
                try:
                    await asyncio.wait_for(worker.process.wait(), timeout=10)
                except TimeoutError:
                    worker.process.kill()
                    await worker.process.wait()
        finally:
            worker.runtime_config_path.unlink(missing_ok=True)

    async def _cleanup_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(min(60, max(5, self.idle_seconds // 2)))
                await self._retire_expired_workers()
        except asyncio.CancelledError:
            return

    async def _retire_expired_workers(self) -> None:
        cutoff = time.monotonic() - self.idle_seconds
        async with self.lock:
            expired = [
                item
                for item in self.workers.values()
                if item.active_requests == 0 and item.last_used < cutoff
            ]
            for worker in expired:
                self.workers.pop(worker.key, None)
                self.retiring_ports.add(worker.port)
        for worker in expired:
            try:
                await self._stop(worker)
            finally:
                async with self.lock:
                    self.retiring_ports.discard(worker.port)

    async def _spawn(self, key: str) -> Worker:
        used_ports = {
            *(worker.port for worker in self.workers.values()),
            *self.retiring_ports,
        }
        port = next(
            value
            for value in range(self.first_port, self.first_port + 1000)
            if value not in used_ports
        )
        workspace = self._workspace(key)
        workspace.mkdir(parents=True, exist_ok=True)
        with Path(self.config_path).open(encoding="utf-8") as source:
            runtime_config = json.load(source)
        defaults = runtime_config.setdefault("agents", {}).setdefault("defaults", {})
        skill_dirs = [str(item) for item in defaults.get("externalSkillsDirs") or []]
        defaults["externalSkillsDirs"] = list(
            dict.fromkeys(["/opt/sagasmith/skills/hosted", *skill_dirs])
        )
        tools = runtime_config.setdefault("tools", {})
        configured = [str(item) for item in tools.get("ssrfWhitelist") or []]
        trusted = trusted_host_cidrs(os.environ.get("SAGASMITH_AGENT_TRUSTED_MCP_HOSTS", ""))
        tools["ssrfWhitelist"] = list(dict.fromkeys([*configured, *trusted]))
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix="sagasmith-agent-", suffix=".json", delete=False
        ) as temporary:
            json.dump(runtime_config, temporary)
            runtime_config_path = Path(temporary.name)
        runtime_config_path.chmod(0o600)
        process = await asyncio.create_subprocess_exec(
            "sagasmith-agent-worker",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workspace",
            str(workspace),
            "--config",
            str(runtime_config_path),
        )
        worker = Worker(
            key=key,
            port=port,
            process=process,
            last_used=time.monotonic(),
            runtime_config_path=runtime_config_path,
        )
        headers = {"Authorization": f"Bearer {self.worker_api_key}"}
        async with httpx.AsyncClient(headers=headers, timeout=1) as client:
            for _ in range(120):
                if process.returncode is not None:
                    raise RuntimeError(f"Agent worker exited with code {process.returncode}")
                try:
                    response = await client.get(f"http://127.0.0.1:{port}/health")
                    if response.status_code == 200:
                        return worker
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.25)
        await self._stop(worker)
        raise RuntimeError("Agent worker did not become ready")

    async def get(self, key: str) -> Worker:
        async with self.lock:
            worker = self.workers.get(key)
            if worker is None or worker.process.returncode is not None:
                worker = await self._spawn(key)
                self.workers[key] = worker
            worker.last_used = time.monotonic()
            return worker

    async def complete(self, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        worker = await self.get(key)
        async with self.lock:
            worker.active_requests += 1
        # Each isolated Nanobot worker has exactly one configured model.  Omitting
        # the OpenAI-compatible selector delegates to that authoritative config
        # instead of coupling Service to a duplicated model name.
        payload.pop("model", None)
        headers = {"Authorization": f"Bearer {self.worker_api_key}"}
        timeout = httpx.Timeout(self.completion_timeout_seconds, connect=5)
        try:
            async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
                response = await client.post(
                    f"http://127.0.0.1:{worker.port}/v1/chat/completions",
                    json={**payload, "session_id": key, "stream": False},
                )
        finally:
            async with self.lock:
                worker.active_requests -= 1
                worker.last_used = time.monotonic()
        if response.status_code >= 400:
            raise RuntimeError(f"Agent worker returned HTTP {response.status_code}")
        value = response.json()
        if not isinstance(value, dict):
            raise RuntimeError("Agent worker returned invalid JSON")
        return value

    async def probe_narrative(self) -> None:
        if self.narrative_control is None:
            raise RuntimeError("Narrative control is not configured")
        if time.monotonic() - self._narrative_probe_success_at < 15:
            return
        async with self._narrative_probe_lock:
            if time.monotonic() - self._narrative_probe_success_at < 15:
                return
            await self.narrative_control.probe()
            self._narrative_probe_success_at = time.monotonic()

    async def execute_narrative(
        self, operation: NarrativeOperation, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        if self.narrative_control is None:
            raise RuntimeError("Narrative control is not configured")
        return await self.narrative_control.execute(operation, arguments)

    def worker_status(self) -> dict[str, Any]:
        tracked = {
            worker.process.pid
            for worker in list(self.workers.values())
            if worker.process.returncode is None and worker.process.pid is not None
        }
        observed: set[int] = set()
        proc = Path("/proc")
        if proc.is_dir():
            for item in proc.iterdir():
                if not item.name.isdigit():
                    continue
                try:
                    command = (item / "cmdline").read_bytes().replace(b"\x00", b" ")
                except OSError:
                    continue
                if b"sagasmith-agent-worker" in command:
                    observed.add(int(item.name))
        return {
            "active_workers": len(tracked),
            "tracked_pids": sorted(tracked),
            "orphan_pids": sorted(observed - tracked),
        }


def create_supervisor_app(manager: WorkerManager, internal_key: str) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await manager.start()
        try:
            yield
        finally:
            await manager.close()

    app = FastAPI(title="SagaSmith Agent Supervisor", docs_url=None, lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/narrative")
    async def narrative_health(
        authorization: str = Header(default=""),
    ) -> dict[str, str]:
        if authorization != f"Bearer {internal_key}":
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid internal credential")
        try:
            await manager.probe_narrative()
        except RuntimeError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        return {"status": "ok"}

    @app.get("/health/workers")
    def worker_health(
        authorization: str = Header(default=""),
    ) -> dict[str, Any]:
        if authorization != f"Bearer {internal_key}":
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid internal credential")
        return {"status": "ok", **manager.worker_status()}

    @app.post("/v1/narrative/operations")
    async def narrative_operation(
        payload: NarrativeOperationRequest,
        authorization: str = Header(default=""),
    ) -> dict[str, Any]:
        if authorization != f"Bearer {internal_key}":
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid internal credential")
        try:
            return await manager.execute_narrative(payload.operation, payload.arguments)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    @app.post("/v1/conversations/{conversation_key}/completions")
    async def complete(
        conversation_key: str,
        payload: CompletionRequest,
        authorization: str = Header(default=""),
    ) -> dict[str, Any]:
        if authorization != f"Bearer {internal_key}":
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid internal credential")
        if len(conversation_key) > 300 or not conversation_key:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid conversation key")
        if not conversation_matches_principal(conversation_key, payload.principal_id):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "conversation principal does not match the authenticated service context",
            )
        try:
            return await manager.complete(conversation_key, payload.model_dump())
        except RuntimeError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    return app


def main() -> None:
    internal_key = os.environ["SAGASMITH_AGENT_INTERNAL_KEY"]
    config_path = os.environ.get("SAGASMITH_AGENT_CONFIG", "/config/agent-config.json")
    manager = WorkerManager(
        config_path=config_path,
        workspace_root=os.environ.get("SAGASMITH_AGENT_WORKSPACES", "/workspaces"),
        worker_api_key=internal_key,
        first_port=int(os.environ.get("SAGASMITH_AGENT_FIRST_PORT", "19000")),
        idle_seconds=int(os.environ.get("SAGASMITH_AGENT_IDLE_SECONDS", "1800")),
        completion_timeout_seconds=int(
            os.environ.get("SAGASMITH_AGENT_COMPLETION_TIMEOUT_SECONDS", "900")
        ),
        narrative_control=NarrativeControlClient.from_agent_config(config_path),
    )
    uvicorn.run(
        create_supervisor_app(manager, internal_key),
        host="0.0.0.0",  # noqa: S104 - only exposed on the private compose network.
        port=int(os.environ.get("SAGASMITH_AGENT_SUPERVISOR_PORT", "8910")),
    )


if __name__ == "__main__":
    main()
