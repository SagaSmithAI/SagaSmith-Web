from __future__ import annotations

import asyncio
import hashlib
import os
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel


class CompletionRequest(BaseModel):
    messages: list[dict[str, Any]]
    model: str = "nanobot"
    stream: bool = False


@dataclass
class Worker:
    key: str
    port: int
    process: asyncio.subprocess.Process
    last_used: float


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
    ) -> None:
        self.config_path = str(Path(config_path).resolve())
        self.workspace_root = Path(workspace_root).resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.worker_api_key = worker_api_key
        self.first_port = first_port
        self.idle_seconds = idle_seconds
        self.workers: dict[str, Worker] = {}
        self.lock = asyncio.Lock()
        self.cleanup_task: asyncio.Task[None] | None = None

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
        if worker.process.returncode is not None:
            return
        worker.process.terminate()
        try:
            await asyncio.wait_for(worker.process.wait(), timeout=10)
        except TimeoutError:
            worker.process.kill()
            await worker.process.wait()

    async def _cleanup_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(min(60, max(5, self.idle_seconds // 2)))
                cutoff = time.monotonic() - self.idle_seconds
                async with self.lock:
                    expired = [item for item in self.workers.values() if item.last_used < cutoff]
                    for worker in expired:
                        self.workers.pop(worker.key, None)
                for worker in expired:
                    await self._stop(worker)
        except asyncio.CancelledError:
            return

    async def _spawn(self, key: str) -> Worker:
        used_ports = {worker.port for worker in self.workers.values()}
        port = next(
            value
            for value in range(self.first_port, self.first_port + 1000)
            if value not in used_ports
        )
        workspace = self._workspace(key)
        workspace.mkdir(parents=True, exist_ok=True)
        process = await asyncio.create_subprocess_exec(
            "nanobot",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workspace",
            str(workspace),
            "--config",
            self.config_path,
        )
        worker = Worker(key=key, port=port, process=process, last_used=time.monotonic())
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
        headers = {"Authorization": f"Bearer {self.worker_api_key}"}
        timeout = httpx.Timeout(240, connect=5)
        async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
            response = await client.post(
                f"http://127.0.0.1:{worker.port}/v1/chat/completions",
                json={**payload, "session_id": key, "stream": False},
            )
        worker.last_used = time.monotonic()
        if response.status_code >= 400:
            raise RuntimeError(f"Agent worker returned HTTP {response.status_code}")
        value = response.json()
        if not isinstance(value, dict):
            raise RuntimeError("Agent worker returned invalid JSON")
        return value


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
        try:
            return await manager.complete(conversation_key, payload.model_dump())
        except RuntimeError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    return app


def main() -> None:
    internal_key = os.environ["SAGASMITH_AGENT_INTERNAL_KEY"]
    manager = WorkerManager(
        config_path=os.environ.get("SAGASMITH_AGENT_CONFIG", "/config/agent-config.json"),
        workspace_root=os.environ.get("SAGASMITH_AGENT_WORKSPACES", "/workspaces"),
        worker_api_key=internal_key,
        first_port=int(os.environ.get("SAGASMITH_AGENT_FIRST_PORT", "19000")),
        idle_seconds=int(os.environ.get("SAGASMITH_AGENT_IDLE_SECONDS", "1800")),
    )
    uvicorn.run(
        create_supervisor_app(manager, internal_key),
        host="0.0.0.0",  # noqa: S104 - only exposed on the private compose network.
        port=int(os.environ.get("SAGASMITH_AGENT_SUPERVISOR_PORT", "8910")),
    )


if __name__ == "__main__":
    main()
