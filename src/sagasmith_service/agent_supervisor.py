from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import math
import os
import re
import shutil
import socket
import stat
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
WORKSPACE_COUNT = Gauge(
    "sagasmith_agent_workspaces",
    "Registered Hosted Agent workspaces on the persistent volume",
)
WORKSPACE_BYTES = Gauge(
    "sagasmith_agent_workspace_bytes",
    "Bytes occupied inside the managed Hosted Agent workspace namespace",
)
WORKSPACE_UNKNOWN_ENTRIES = Gauge(
    "sagasmith_agent_workspace_unknown_entries",
    "Unregistered entries ignored by Hosted Agent workspace cleanup",
)
WORKSPACE_CLEANUP = Counter(
    "sagasmith_agent_workspace_cleanup_total",
    "Registered Hosted Agent workspace cleanup outcomes",
    ["reason", "status"],
)

LOGGER = logging.getLogger(__name__)
WORKSPACE_NAMESPACE = "hosted-v1"
WORKSPACE_ROOT_MARKER = ".sagasmith-workspace-root.json"
WORKSPACE_MARKER = ".sagasmith-workspace.json"
WORKSPACE_ROOT_SCHEMA = "sagasmith.agent-workspace-root/v1"
WORKSPACE_SCHEMA = "sagasmith.agent-workspace/v1"
WORKSPACE_OWNER = "sagasmith-web-agent-supervisor"
WORKSPACE_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")


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
    terminal_requested: bool = False


@dataclass(frozen=True)
class WorkspaceRecord:
    workspace_id: str
    path: Path
    state: str
    created_at: float
    last_used_at: float
    size_bytes: int


@dataclass(frozen=True)
class WorkspaceSnapshot:
    records: tuple[WorkspaceRecord, ...]
    total_bytes: int
    unknown_entries: int
    unknown_bytes: int


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
        workspace_ttl_seconds: int = 7 * 24 * 60 * 60,
        max_workspaces: int = 256,
        workspace_max_bytes: int = 20 * 1024 * 1024 * 1024,
        narrative_control: NarrativeControlClient | None = None,
    ) -> None:
        self.config_path = str(Path(config_path).resolve())
        self.workspace_root = Path(workspace_root).resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.managed_workspace_root = self.workspace_root / WORKSPACE_NAMESPACE
        self.worker_service_token = worker_service_token or worker_api_key or ""
        self.first_port = first_port
        self.idle_seconds = idle_seconds
        self.completion_timeout_seconds = max(30, int(completion_timeout_seconds))
        self.max_workers = max(1, int(max_workers))
        self.spawn_concurrency = max(1, int(spawn_concurrency))
        self.workspace_ttl_seconds = max(1, int(workspace_ttl_seconds))
        self.max_workspaces = max(1, int(max_workspaces))
        self.workspace_max_bytes = max(1, int(workspace_max_bytes))
        self.narrative_control = narrative_control
        self.workers: dict[str, Worker] = {}
        self.retiring_ports: set[int] = set()
        self.reserved_ports: set[int] = set()
        self.spawn_tasks: dict[str, asyncio.Task[Worker]] = {}
        self.lock = asyncio.Lock()
        self.workspace_lock = asyncio.Lock()
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
        self._workspace_store_initialized = False
        self._workspace_snapshot = WorkspaceSnapshot((), 0, 0, 0)

    def _workspace(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode()).hexdigest()
        return self.managed_workspace_root / digest

    async def start(self) -> None:
        self._closing = False
        async with self.workspace_lock:
            await asyncio.to_thread(self._initialize_workspace_store)
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

    async def _stop(self, worker: Worker, *, delete_workspace: bool = False) -> None:
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
        async with self.workspace_lock:
            if delete_workspace:
                await asyncio.to_thread(
                    self._set_workspace_state,
                    self._workspace(worker.key),
                    "terminal",
                )
                await asyncio.to_thread(
                    self._delete_registered_workspace,
                    self._workspace(worker.key),
                    "terminal",
                )
            else:
                await asyncio.to_thread(self._mark_workspace_idle, self._workspace(worker.key))

    async def _cleanup_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(min(60, max(5, self.idle_seconds // 2)))
                try:
                    await self._retire_expired_workers()
                except WorkerCapacityError:
                    LOGGER.warning("Hosted workspace cleanup is blocked by protected capacity")
                except Exception:
                    LOGGER.exception("Hosted workspace cleanup failed")
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
        async with self.workspace_lock:
            protected = self._protected_workspace_ids()
            await asyncio.to_thread(self._prune_workspace_store, protected, 0)

    def _protected_workspace_ids(self) -> set[str]:
        return {
            *(self._workspace(key).name for key in self.workers),
            *(self._workspace(key).name for key in self.spawn_tasks),
        }

    @staticmethod
    def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f"{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temporary:
            json.dump(value, temporary, sort_keys=True, separators=(",", ":"))
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        try:
            temporary_path.chmod(0o600)
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _initialize_workspace_store(self) -> None:
        if self._workspace_store_initialized:
            return
        namespace = self.managed_workspace_root
        if namespace.exists() or self._is_link_like(namespace):
            metadata = namespace.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or self._is_link_like(namespace):
                raise RuntimeError("Hosted workspace namespace must be a real directory")
            marker_path = namespace / WORKSPACE_ROOT_MARKER
            marker = self._read_json_file(marker_path)
            if marker != {
                "schema": WORKSPACE_ROOT_SCHEMA,
                "owner": WORKSPACE_OWNER,
            }:
                raise RuntimeError("Hosted workspace namespace is not registered to this service")
        else:
            namespace.mkdir(mode=0o700)
            self._write_json_atomic(
                namespace / WORKSPACE_ROOT_MARKER,
                {"schema": WORKSPACE_ROOT_SCHEMA, "owner": WORKSPACE_OWNER},
            )

        snapshot = self._scan_workspace_store()
        if snapshot.unknown_entries:
            LOGGER.warning(
                "Ignored unregistered entries in the Hosted workspace namespace",
                extra={
                    "workspace_unknown_entries": snapshot.unknown_entries,
                    "workspace_unknown_bytes": snapshot.unknown_bytes,
                },
            )
        for record in snapshot.records:
            if record.state == "active":
                self._write_workspace_marker(
                    record.path,
                    state="idle",
                    created_at=record.created_at,
                    last_used_at=record.last_used_at,
                )
                WORKSPACE_CLEANUP.labels(reason="crash_recovery", status="retained").inc()
                LOGGER.info(
                    "Recovered crash-left active Hosted workspace as idle",
                    extra={"workspace_id": record.workspace_id},
                )
        self._prune_workspace_store(set(), 0)
        self._workspace_store_initialized = True

    @staticmethod
    def _is_link_like(path: Path) -> bool:
        try:
            return path.is_symlink() or path.is_junction()
        except OSError:
            return True

    @staticmethod
    def _read_json_file(path: Path) -> dict[str, Any] | None:
        try:
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or WorkerManager._is_link_like(path)
                or metadata.st_size <= 0
                or metadata.st_size > 4096
            ):
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    def _registered_workspace(self, path: Path) -> WorkspaceRecord | None:
        try:
            if path.parent != self.managed_workspace_root:
                return None
            if WORKSPACE_ID_PATTERN.fullmatch(path.name) is None:
                return None
            metadata = path.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or self._is_link_like(path):
                return None
        except OSError:
            return None
        marker = self._read_json_file(path / WORKSPACE_MARKER)
        if marker is None:
            return None
        if (
            marker.get("schema") != WORKSPACE_SCHEMA
            or marker.get("owner") != WORKSPACE_OWNER
            or marker.get("workspace_id") != path.name
            or marker.get("state") not in {"active", "idle", "terminal"}
        ):
            return None
        try:
            created_value = marker["created_at"]
            last_used_value = marker["last_used_at"]
            if (
                isinstance(created_value, bool)
                or not isinstance(created_value, (int, float))
                or isinstance(last_used_value, bool)
                or not isinstance(last_used_value, (int, float))
            ):
                return None
            created_at = float(created_value)
            last_used_at = float(last_used_value)
            if (
                not math.isfinite(created_at)
                or not math.isfinite(last_used_at)
                or created_at <= 0
                or last_used_at < created_at
            ):
                return None
        except (KeyError, TypeError, ValueError):
            return None
        return WorkspaceRecord(
            workspace_id=path.name,
            path=path,
            state=str(marker["state"]),
            created_at=created_at,
            last_used_at=last_used_at,
            size_bytes=self._directory_size(path),
        )

    @staticmethod
    def _directory_size(root: Path) -> int:
        total = 0
        pending = [root]
        while pending:
            directory = pending.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError:
                continue
            for entry in entries:
                try:
                    if entry.is_symlink():
                        total += entry.stat(follow_symlinks=False).st_size
                    elif entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
        return total

    def _scan_workspace_store(self) -> WorkspaceSnapshot:
        records: list[WorkspaceRecord] = []
        unknown = 0
        unknown_bytes = 0
        try:
            entries = list(self.managed_workspace_root.iterdir())
        except OSError:
            entries = []
        for path in entries:
            if path.name == WORKSPACE_ROOT_MARKER:
                continue
            record = self._registered_workspace(path)
            if record is None:
                unknown += 1
                try:
                    metadata = path.lstat()
                    if stat.S_ISDIR(metadata.st_mode) and not self._is_link_like(path):
                        unknown_bytes += self._directory_size(path)
                    else:
                        unknown_bytes += metadata.st_size
                except OSError:
                    pass
            else:
                records.append(record)
        snapshot = WorkspaceSnapshot(
            records=tuple(records),
            total_bytes=sum(item.size_bytes for item in records) + unknown_bytes,
            unknown_entries=unknown,
            unknown_bytes=unknown_bytes,
        )
        self._set_workspace_snapshot(snapshot)
        return snapshot

    def _set_workspace_snapshot(self, snapshot: WorkspaceSnapshot) -> None:
        self._workspace_snapshot = snapshot
        WORKSPACE_COUNT.set(len(snapshot.records))
        WORKSPACE_BYTES.set(snapshot.total_bytes)
        WORKSPACE_UNKNOWN_ENTRIES.set(snapshot.unknown_entries)

    def _refresh_workspace_record(self, path: Path) -> None:
        record = self._registered_workspace(path)
        if record is None:
            return
        records = {
            item.workspace_id: item
            for item in self._workspace_snapshot.records
            if item.workspace_id != record.workspace_id
        }
        records[record.workspace_id] = record
        snapshot = WorkspaceSnapshot(
            records=tuple(records.values()),
            total_bytes=sum(item.size_bytes for item in records.values())
            + self._workspace_snapshot.unknown_bytes,
            unknown_entries=self._workspace_snapshot.unknown_entries,
            unknown_bytes=self._workspace_snapshot.unknown_bytes,
        )
        self._set_workspace_snapshot(snapshot)

    def _write_workspace_marker(
        self,
        path: Path,
        *,
        state: str,
        created_at: float,
        last_used_at: float,
    ) -> None:
        if path.parent != self.managed_workspace_root:
            raise RuntimeError("Hosted workspace path escaped its registered namespace")
        self._write_json_atomic(
            path / WORKSPACE_MARKER,
            {
                "schema": WORKSPACE_SCHEMA,
                "owner": WORKSPACE_OWNER,
                "workspace_id": path.name,
                "state": state,
                "created_at": created_at,
                "last_used_at": last_used_at,
            },
        )

    def _mark_workspace_idle(self, path: Path) -> None:
        self._set_workspace_state(path, "idle")
        self._refresh_workspace_record(path)

    def _set_workspace_state(self, path: Path, state: str) -> None:
        record = self._registered_workspace(path)
        if record is None:
            return
        self._write_workspace_marker(
            path,
            state=state,
            created_at=record.created_at,
            last_used_at=max(record.last_used_at, time.time()),
        )

    def _touch_workspace(self, path: Path, *, state: str = "active") -> None:
        record = self._registered_workspace(path)
        if record is None:
            raise WorkerCapacityError("Hosted workspace registration is missing or invalid")
        self._write_workspace_marker(
            path,
            state=state,
            created_at=record.created_at,
            last_used_at=time.time(),
        )
        self._refresh_workspace_record(path)

    def _workspace_tree_is_safe_to_delete(self, root: Path) -> bool:
        if self._is_link_like(root) or os.path.ismount(root):
            return False
        pending = [root]
        while pending:
            directory = pending.pop()
            try:
                entries = list(os.scandir(directory))
            except OSError:
                return False
            for entry in entries:
                path = Path(entry.path)
                try:
                    if entry.is_symlink() or self._is_link_like(path) or os.path.ismount(path):
                        return False
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(path)
                except OSError:
                    return False
        return True

    def _delete_registered_workspace(self, path: Path, reason: str) -> bool:
        record = self._registered_workspace(path)
        if record is None:
            WORKSPACE_CLEANUP.labels(reason=reason, status="refused").inc()
            LOGGER.warning(
                "Refused to delete an unregistered Hosted workspace",
                extra={"workspace_entry": path.name, "reason": reason},
            )
            return False
        if not self._workspace_tree_is_safe_to_delete(path):
            WORKSPACE_CLEANUP.labels(reason=reason, status="refused").inc()
            LOGGER.warning(
                "Refused to delete a registered Hosted workspace with unsafe links or mounts",
                extra={"workspace_id": record.workspace_id, "reason": reason},
            )
            return False
        try:
            shutil.rmtree(path)
        except OSError:
            WORKSPACE_CLEANUP.labels(reason=reason, status="error").inc()
            LOGGER.exception(
                "Failed to delete registered Hosted workspace",
                extra={"workspace_id": record.workspace_id, "reason": reason},
            )
            return False
        WORKSPACE_CLEANUP.labels(reason=reason, status="deleted").inc()
        LOGGER.info(
            "Deleted registered Hosted workspace",
            extra={
                "workspace_id": record.workspace_id,
                "workspace_bytes": record.size_bytes,
                "reason": reason,
            },
        )
        self._scan_workspace_store()
        return True

    def _prune_workspace_store(
        self,
        protected_ids: set[str],
        required_slots: int,
    ) -> WorkspaceSnapshot:
        snapshot = self._scan_workspace_store()
        now = time.time()
        terminal = sorted(
            (
                item
                for item in snapshot.records
                if item.workspace_id not in protected_ids and item.state == "terminal"
            ),
            key=lambda item: item.last_used_at,
        )
        for record in terminal:
            self._delete_registered_workspace(record.path, "terminal")
        expired = sorted(
            (
                item
                for item in snapshot.records
                if item.workspace_id not in protected_ids
                and now - item.last_used_at >= self.workspace_ttl_seconds
            ),
            key=lambda item: item.last_used_at,
        )
        for record in expired:
            self._delete_registered_workspace(record.path, "ttl")

        while True:
            snapshot = self._scan_workspace_store()
            count_over = (
                len(snapshot.records) + snapshot.unknown_entries + required_slots
                > self.max_workspaces
            )
            bytes_over = snapshot.total_bytes > self.workspace_max_bytes
            if not count_over and not bytes_over:
                return snapshot
            candidates = sorted(
                (item for item in snapshot.records if item.workspace_id not in protected_ids),
                key=lambda item: item.last_used_at,
            )
            if not candidates:
                WORKSPACE_CLEANUP.labels(reason="capacity", status="refused").inc()
                raise WorkerCapacityError(
                    "Hosted workspace capacity is full; retry after active work completes"
                )
            if not self._delete_registered_workspace(candidates[0].path, "lru"):
                raise WorkerCapacityError(
                    "Hosted workspace cleanup failed; retry after operator review"
                )

    async def _prepare_workspace(self, key: str) -> Path:
        workspace = self._workspace(key)
        workspace_id = workspace.name
        async with self.workspace_lock:
            if not self._workspace_store_initialized:
                await asyncio.to_thread(self._initialize_workspace_store)
            record = await asyncio.to_thread(self._registered_workspace, workspace)
            protected = self._protected_workspace_ids() | {workspace_id}
            required_slots = 0 if record is not None else 1
            await asyncio.to_thread(
                self._prune_workspace_store,
                protected,
                required_slots,
            )
            created = False
            if workspace.exists() or self._is_link_like(workspace):
                if record is None:
                    raise WorkerCapacityError(
                        "Hosted workspace path exists without a valid service registration"
                    )
            else:
                workspace.mkdir(mode=0o700)
                created = True
                now = time.time()
                await asyncio.to_thread(
                    self._write_workspace_marker,
                    workspace,
                    state="active",
                    created_at=now,
                    last_used_at=now,
                )
            await asyncio.to_thread(self._touch_workspace, workspace)
            try:
                await asyncio.to_thread(
                    self._prune_workspace_store,
                    protected,
                    0,
                )
            except WorkerCapacityError:
                if created:
                    await asyncio.to_thread(
                        self._delete_registered_workspace,
                        workspace,
                        "capacity_rollback",
                    )
                raise
        return workspace

    async def _enforce_workspace_capacity(self) -> None:
        if (
            len(self._workspace_snapshot.records) + self._workspace_snapshot.unknown_entries
            <= self.max_workspaces
            and self._workspace_snapshot.total_bytes <= self.workspace_max_bytes
        ):
            return
        async with self.workspace_lock:
            protected = self._protected_workspace_ids()
            await asyncio.to_thread(self._prune_workspace_store, protected, 0)

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
        workspace = await self._prepare_workspace(key)
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
                if (
                    worker is not None
                    and worker.process.returncode is None
                    and not worker.terminal_requested
                ):
                    worker.last_used = time.monotonic()
                    if reserve_request:
                        worker.active_requests += 1
                    self._refresh_worker_count_metrics()
                    return worker

                if worker is not None and worker.terminal_requested:
                    raise WorkerCapacityError(
                        "Agent conversation is terminating; retry with a new conversation"
                    )

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
        if not payload.get("terminal"):
            await self._enforce_workspace_capacity()
        worker = await self._get(key, reserve_request=True)
        # Each isolated Nanobot worker has exactly one configured model.  Omitting
        # the OpenAI-compatible selector delegates to that authoritative config
        # instead of coupling Service to a duplicated model name.
        payload.pop("model", None)
        terminal_succeeded = False
        retiring: Worker | None = None
        try:
            response = await self.completion_client.post(
                f"http://127.0.0.1:{worker.port}/v1/chat/completions",
                json={**payload, "session_id": key, "stream": False},
            )
            if response.status_code >= 400:
                raise RuntimeError(f"Agent worker returned HTTP {response.status_code}")
            value = response.json()
            if not isinstance(value, dict):
                raise RuntimeError("Agent worker returned invalid JSON")
            terminal_succeeded = bool(payload.get("terminal"))
            return value
        finally:
            async with self.lock:
                worker.active_requests -= 1
                worker.last_used = time.monotonic()
                if terminal_succeeded:
                    worker.terminal_requested = True
                if (
                    worker.terminal_requested
                    and worker.active_requests == 0
                    and self.workers.get(worker.key) is worker
                ):
                    retiring = self.workers.pop(worker.key)
                    self.retiring_ports.add(worker.port)
                self._refresh_worker_count_metrics()
            if retiring is not None:
                try:
                    await self._stop(retiring, delete_workspace=True)
                finally:
                    async with self.lock:
                        self.retiring_ports.discard(retiring.port)
                        self._refresh_worker_count_metrics()
            elif not terminal_succeeded:
                async with self.workspace_lock:
                    await asyncio.to_thread(self._touch_workspace, self._workspace(worker.key))

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
            "workspace_count": len(self._workspace_snapshot.records),
            "workspace_capacity": self.max_workspaces,
            "workspace_bytes": self._workspace_snapshot.total_bytes,
            "workspace_byte_capacity": self.workspace_max_bytes,
            "workspace_unknown_entries": self._workspace_snapshot.unknown_entries,
            "workspace_unknown_bytes": self._workspace_snapshot.unknown_bytes,
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
    boundary_mode = os.environ.get("SAGASMITH_AGENT_BOUNDARY_MODE", "modern")
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
        workspace_ttl_seconds=int(
            os.environ.get("SAGASMITH_AGENT_WORKSPACE_TTL_SECONDS", "604800")
        ),
        max_workspaces=int(os.environ.get("SAGASMITH_AGENT_MAX_WORKSPACES", "256")),
        workspace_max_bytes=int(
            os.environ.get("SAGASMITH_AGENT_WORKSPACE_MAX_BYTES", "21474836480")
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
