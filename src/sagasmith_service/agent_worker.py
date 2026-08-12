from __future__ import annotations

import argparse
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel


class WorkerCompletionRequest(BaseModel):
    messages: list[dict[str, Any]]
    session_id: str
    principal_id: str
    stream: bool = False


def create_worker_app(agent_loop: Any, model_name: str) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await agent_loop._connect_mcp()
        try:
            yield
        finally:
            await agent_loop.close_mcp()

    app = FastAPI(title="SagaSmith Hosted Agent Worker", docs_url=None, lifespan=lifespan)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    async def complete(payload: WorkerCompletionRequest) -> dict[str, Any]:
        if payload.stream:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "streaming is unsupported")
        if len(payload.messages) != 1 or payload.messages[0].get("role") != "user":
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "one user message required")
        principal_id = payload.principal_id.strip()
        if not principal_id.startswith("user:") or len(principal_id) > 160:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid principal")
        content = payload.messages[0].get("content")
        if not isinstance(content, str):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "text content required")
        response = await agent_loop.process_direct(
            content=content,
            session_key=f"service:{payload.session_id}",
            channel="user",
            sender_id=principal_id.removeprefix("user:"),
        )
        usage = getattr(agent_loop, "_last_usage", None) or {}
        response_text = str(getattr(response, "content", response) or "")
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": response_text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
            },
        }

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    from nanobot.agent.hooks import create_file_edit_activity_hook
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.config.loader import load_config, resolve_config_env_vars, set_config_path
    from nanobot.providers.image_generation import image_gen_provider_configs
    from nanobot.session.manager import SessionManager
    from nanobot.utils.helpers import sync_workspace_templates

    config_path = Path(args.config).resolve()
    set_config_path(config_path)
    config = resolve_config_env_vars(load_config(config_path))
    config.agents.defaults.workspace = args.workspace
    sync_workspace_templates(config.workspace_path)
    loop = AgentLoop.from_config(
        config,
        MessageBus(),
        session_manager=SessionManager(config.workspace_path),
        image_generation_provider_configs=image_gen_provider_configs(config),
        hook_factories=[create_file_edit_activity_hook],
    )
    model_name = config.resolve_preset().model
    uvicorn.run(create_worker_app(loop, model_name), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
