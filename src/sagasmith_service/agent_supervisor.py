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
from fastapi.responses import Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel, ConfigDict

from sagasmith_service.narrative_control import NarrativeControlClient, NarrativeOperation

WORKER_SPAWN_SECONDS = Histogram(
    "sagasmith_agent_worker_spawn_seconds",
    "Time spent starting an isolated Agent worker",
    ["status"],
)
WORKER_COUNT = Gauge(
    "sagasmith_agent_workers",
    "Supervisor worker lifecycle counts",
    ["state"],
)
WORKER_RSS_BYTES = Gauge(
    "sagasmith_agent_worker_rss_bytes",
    "Resident memory of tracked Agent workers",
)
WORKER_SPAWN_QUEUE = Gauge(
    "sagasmith_agent_worker_spawn_queue",
    "Agent workers waiting for a bounded spawn slot",
)
WORKER_CAPACITY_REJECTIONS = Counter(
    "sagasmith_agent_worker_capacity_rejections_total",
    "Completions rejected because every bounded worker slot was busy",
)


class CompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[dict[str, Any]]
    principal_id: str | None = None
    trusted_context: dict[str, Any] | None = None
    stream: bool = False
    response_contract: dict[str, Any] | None = None
    terminal: bool = False
    idempotency_key: str | None = None


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


class WorkerCapacityError(RuntimeError):
    """Raised when bounded worker capacity has no safe LRU eviction candidate."""


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
    """One bounded worker process and MCP connection set per conversation.

    Modern MCP requests carry authority explicitly and do not treat a protocol
    session as an identity boundary. Legacy workers may still use their
    connection-local adapter while the component lock remains on that era.
    """

    def __init__(
        self,
        *,
        config_path: str,
        workspace_root: str,
        worker_api_key: str | None = None,
        worker_service_token: str | None = None,
        first_port: int = 19000,
        idle_seconds: int = 1800,
        completion_timeout_seconds: int = 900,
        max_workers: int = 64,
        spawn_concurrency: int = 4,
        narrative_control: NarrativeControlClient | None = None,
    ) -> None:
        self.config_path = str(Path(config_path).resolve())
        self.workspace_root = Path(workspace_root).resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.worker_service_token = worker_service_token or worker_api_key or ""
        self.first_port = first_port
        self.idle_seconds = idle_seconds
        self.completion_timeout_seconds = max(30, int(completion_timeout_seconds))
        self.max_workers = max(1, int(max_workers))
        self.spawn_concurrency = max(1, int(spawn_concurrency))
        self.narrative_control = narrative_control
        self.workers: dict[str, Worker] = {}
        self.retiring_ports: set[int] = set()
        self.reserved_ports: set[int] = set()
        self.spawn_tasks: dict[str, asyncio.Task[Worker]] = {}
        self.lock = asyncio.Lock()
        self.spawn_semaphore = asyncio.Semaphore(self.spawn_concurrency)
        self.cleanup_task: asyncio.Task[None] | None = None
        self._narrative_probe_lock = asyncio.Lock()
        self._narrative_probe_success_at = 0.0
        headers = {"Authorization": f"Bearer {self.worker_service_token}"}
        self.readiness_client = httpx.AsyncClient(headers=headers, timeout=1)
        self.completion_client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(self.completion_timeout_seconds, connect=5),
        )
        self._closing = False

    def _workspace(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode()).hexdigest()
        return self.workspace_root / digest

    async def start(self) -> None:
        self._closing = False
        self.cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def close(self) -> None:
        self._closing = True
        if self.cleanup_task is not None:
            self.cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.cleanup_task
        async with self.lock:
            spawn_tasks = list(self.spawn_tasks.values())
            for task in spawn_tasks:
                task.cancel()
            workers = list(self.workers.values())
            self.workers.clear()
        if spawn_tasks:
            await asyncio.gather(*spawn_tasks, return_exceptions=True)
        for worker in workers:
            await self._stop(worker)
        await asyncio.gather(
            self.readiness_client.aclose(),
            self.completion_client.aclose(),
            return_exceptions=True,
        )
        self._refresh_worker_count_metrics()

    async def _stop(self, worker: Worker) -> None:
        try:
            if worker.process.returncode is None:
                worker.process.terminate()
                try:
                    await asyncio.wait_for(worker.process.wait(), timeout=10)
                except TimeoutError:
                    worker.process.kill()
                    await worker.process.wait()
                except asyncio.CancelledError:
                    if worker.process.returncode is None:
                        worker.process.kill()
                        with suppress(asyncio.CancelledError, OSError):
                            await worker.process.wait()
                    raise
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
            self._refresh_worker_count_metrics()
        for worker in expired:
            try:
                await self._stop(worker)
            finally:
                async with self.lock:
                    self.retiring_ports.discard(worker.port)
                    self._refresh_worker_count_metrics()

    def _next_port(self) -> int:
        used_ports = {
            *(worker.port for worker in self.workers.values()),
            *self.retiring_ports,
            *self.reserved_ports,
        }
        try:
            return next(
                value
                for value in range(self.first_port, self.first_port + 1000)
                if value not in used_ports
            )
        except StopIteration as exc:
            raise WorkerCapacityError("Agent worker port capacity is exhausted") from exc

    def _refresh_worker_count_metrics(self) -> None:
        WORKER_COUNT.labels(state="ready").set(len(self.workers))
        WORKER_COUNT.labels(state="busy").set(
            sum(worker.active_requests > 0 for worker in self.workers.values())
        )
        WORKER_COUNT.labels(state="spawning").set(len(self.spawn_tasks))
        WORKER_COUNT.labels(state="retiring").set(len(self.retiring_ports))

    async def _spawn(self, key: str, port: int) -> Worker:
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
        worker: Worker | None = None
        try:
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
            for _ in range(120):
                if process.returncode is not None:
                    raise RuntimeError(f"Agent worker exited with code {process.returncode}")
                try:
                    response = await self.readiness_client.get(f"http://127.0.0.1:{port}/health")
                    if response.status_code == 200:
                        return worker
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.25)
            raise RuntimeError("Agent worker did not become ready")
        except BaseException:
            if worker is not None:
                await self._stop(worker)
            else:
                runtime_config_path.unlink(missing_ok=True)
            raise

    async def _spawn_for_key(
        self,
        key: str,
        port: int,
        evicted: Worker | None,
    ) -> Worker:
        started = time.perf_counter()
        outcome = "error"
        WORKER_SPAWN_QUEUE.inc()
        acquired = False
        worker: Worker | None = None
        installed = False
        try:
            if evicted is not None:
                try:
                    await self._stop(evicted)
                finally:
                    async with self.lock:
                        self.retiring_ports.discard(evicted.port)
                        self._refresh_worker_count_metrics()
            await self.spawn_semaphore.acquire()
            acquired = True
            WORKER_SPAWN_QUEUE.dec()
            worker = await self._spawn(key, port)
            async with self.lock:
                closing = self._closing
                if not closing:
                    self.workers[key] = worker
                    installed = True
                    self._refresh_worker_count_metrics()
            if closing:
                await self._stop(worker)
                worker = None
                raise RuntimeError("Agent supervisor is shutting down")
            outcome = "success"
            return worker
        except BaseException:
            if worker is not None and not installed:
                await self._stop(worker)
            raise
        finally:
            if not acquired:
                WORKER_SPAWN_QUEUE.dec()
            else:
                self.spawn_semaphore.release()
            async with self.lock:
                current = asyncio.current_task()
                if self.spawn_tasks.get(key) is current:
                    self.spawn_tasks.pop(key, None)
                self.reserved_ports.discard(port)
                self._refresh_worker_count_metrics()
            WORKER_SPAWN_SECONDS.labels(status=outcome).observe(time.perf_counter() - started)

    async def _get(self, key: str, *, reserve_request: bool) -> Worker:
        while True:
            async with self.lock:
                if self._closing:
                    raise RuntimeError("Agent supervisor is shutting down")
                worker = self.workers.get(key)
                if worker is not None and worker.process.returncode is None:
                    worker.last_used = time.monotonic()
                    if reserve_request:
                        worker.active_requests += 1
                    self._refresh_worker_count_metrics()
                    return worker

                stale = None
                if worker is not None:
                    stale = self.workers.pop(key)
                    self.retiring_ports.add(stale.port)

                task = self.spawn_tasks.get(key)
                if task is None:
                    evicted = stale
                    allocated = len(self.workers) + len(self.spawn_tasks)
                    if allocated >= self.max_workers:
                        candidates = [
                            item for item in self.workers.values() if item.active_requests == 0
                        ]
                        if not candidates:
                            WORKER_CAPACITY_REJECTIONS.inc()
                            raise WorkerCapacityError(
                                "Agent worker capacity is full; retry after another completion"
                            )
                        candidate = min(candidates, key=lambda item: item.last_used)
                        self.workers.pop(candidate.key, None)
                        self.retiring_ports.add(candidate.port)
                        evicted = candidate
                    port = self._next_port()
                    self.reserved_ports.add(port)
                    task = asyncio.create_task(self._spawn_for_key(key, port, evicted))
                    self.spawn_tasks[key] = task
                    self._refresh_worker_count_metrics()
            await asyncio.shield(task)

    async def get(self, key: str) -> Worker:
        return await self._get(key, reserve_request=False)

    async def complete(self, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        worker = await self._get(key, reserve_request=True)
        # Each isolated Nanobot worker has exactly one configured model.  Omitting
        # the OpenAI-compatible selector delegates to that authoritative config
        # instead of coupling Service to a duplicated model name.
        payload.pop("model", None)
        try:
            response = await self.completion_client.post(
                f"http://127.0.0.1:{worker.port}/v1/chat/completions",
                json={**payload, "session_id": key, "stream": False},
            )
        finally:
            async with self.lock:
                worker.active_requests -= 1
                worker.last_used = time.monotonic()
                self._refresh_worker_count_metrics()
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
        worker_snapshot = list(self.workers.values())
        tracked = {
            worker.process.pid
            for worker in worker_snapshot
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
        rss_bytes = 0
        for pid in tracked:
            try:
                status_text = (proc / str(pid) / "status").read_text(encoding="utf-8")
                rss_line = next(
                    line for line in status_text.splitlines() if line.startswith("VmRSS:")
                )
                rss_bytes += int(rss_line.split()[1]) * 1024
            except (OSError, StopIteration, ValueError):
                continue
        WORKER_RSS_BYTES.set(rss_bytes)
        WORKER_COUNT.labels(state="ready").set(len(worker_snapshot))
        WORKER_COUNT.labels(state="busy").set(
            sum(worker.active_requests > 0 for worker in worker_snapshot)
        )
        WORKER_COUNT.labels(state="spawning").set(len(self.spawn_tasks))
        WORKER_COUNT.labels(state="retiring").set(len(self.retiring_ports))
        return {
            "active_workers": len(tracked),
            "busy_workers": sum(worker.active_requests > 0 for worker in worker_snapshot),
            "spawning_workers": len(self.spawn_tasks),
            "worker_capacity": self.max_workers,
            "worker_rss_bytes": rss_bytes,
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

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        manager.worker_status()
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

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
        trusted = payload.trusted_context or {}
        trusted_principals = {
            value
            for value in (
                payload.principal_id,
                trusted.get("requester_principal"),
                trusted.get("acting_host_principal"),
            )
            if isinstance(value, str) and value
        }
        if not trusted_principals or not any(
            conversation_matches_principal(conversation_key, principal)
            for principal in trusted_principals
        ):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "conversation principal does not match the authenticated service context",
            )
        try:
            return await manager.complete(
                conversation_key,
                payload.model_dump(exclude_none=True),
            )
        except WorkerCapacityError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    return app


def main() -> None:
    internal_key = os.environ["SAGASMITH_AGENT_INTERNAL_KEY"]
    worker_service_token = os.environ.get("SAGASMITH_WORKER_SERVICE_TOKEN", "")
    boundary_mode = os.environ.get("SAGASMITH_AGENT_BOUNDARY_MODE", "legacy")
    if boundary_mode not in {"legacy", "modern"}:
        raise RuntimeError("SAGASMITH_AGENT_BOUNDARY_MODE must be legacy or modern")
    if boundary_mode == "modern" and len(worker_service_token.encode("utf-8")) < 32:
        raise RuntimeError("SAGASMITH_WORKER_SERVICE_TOKEN must contain at least 32 bytes")
    child_token = worker_service_token if boundary_mode == "modern" else internal_key
    config_path = os.environ.get("SAGASMITH_AGENT_CONFIG", "/config/agent-config.json")
    manager = WorkerManager(
        config_path=config_path,
        workspace_root=os.environ.get("SAGASMITH_AGENT_WORKSPACES", "/workspaces"),
        worker_service_token=child_token,
        first_port=int(os.environ.get("SAGASMITH_AGENT_FIRST_PORT", "19000")),
        idle_seconds=int(os.environ.get("SAGASMITH_AGENT_IDLE_SECONDS", "1800")),
        completion_timeout_seconds=int(
            os.environ.get("SAGASMITH_AGENT_COMPLETION_TIMEOUT_SECONDS", "900")
        ),
        max_workers=int(os.environ.get("SAGASMITH_AGENT_MAX_WORKERS", "64")),
        spawn_concurrency=int(os.environ.get("SAGASMITH_AGENT_SPAWN_CONCURRENCY", "4")),
        narrative_control=NarrativeControlClient.from_agent_config(config_path),
    )
    uvicorn.run(
        create_supervisor_app(manager, internal_key),
        host="0.0.0.0",  # noqa: S104 - only exposed on the private compose network.
        port=int(os.environ.get("SAGASMITH_AGENT_SUPERVISOR_PORT", "8910")),
    )


if __name__ == "__main__":
    main()
