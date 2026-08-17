from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel


class WorkerCompletionRequest(BaseModel):
    messages: list[dict[str, Any]]
    session_id: str
    principal_id: str
    stream: bool = False
    response_contract: dict[str, Any] | None = None


def create_worker_app(agent_loop: Any, model_name: str) -> FastAPI:
    turn_guard = asyncio.Lock()

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
        principal_parts = principal_id.split(":", 1)
        if (
            len(principal_parts) != 2
            or principal_parts[0] not in {"user", "agent"}
            or not principal_parts[1]
            or len(principal_id) > 160
        ):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid principal")
        content = payload.messages[0].get("content")
        if not isinstance(content, str):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "text content required")
        async with turn_guard:
            structured_tool = None
            activity_tool = None
            receipt_hook = None
            turn_tools = None
            registered_tools = None
            if payload.response_contract is not None:
                contract = payload.response_contract
                try:
                    from nanobot.agent.hook import AgentHook
                    from nanobot.agent.resolution_presentation import (
                        normalize_resolution_presentation,
                    )
                    from nanobot.agent.tools.structured_output import StructuredOutputTool

                    terminal_contract = dict(contract.get("terminal") or contract)
                    activity_contract = contract.get("activity")
                    callback = dict(contract.get("activity_callback") or {})

                    if activity_contract is not None:
                        from nanobot.agent.tools.base import Tool, ToolResult

                        class RoomActivityTool(Tool):
                            def __init__(self) -> None:
                                self._name = str(activity_contract["name"])
                                self._description = str(activity_contract["description"])
                                self._parameters = dict(activity_contract["parameters"])

                            @property
                            def name(self) -> str:
                                return self._name

                            @property
                            def description(self) -> str:
                                return self._description

                            @property
                            def parameters(self) -> dict[str, Any]:
                                return dict(self._parameters)

                            @property
                            def exclusive(self) -> bool:
                                return True

                            async def execute(self, **kwargs: Any) -> ToolResult:
                                async with httpx.AsyncClient(timeout=10) as client:
                                    response = await client.post(
                                        str(callback["url"]),
                                        headers={
                                            "Authorization": f"Bearer {callback['token']}"
                                        },
                                        json=kwargs,
                                    )
                                if response.status_code >= 400:
                                    return ToolResult.error(
                                        "Room activity was rejected by the host."
                                    )
                                return ToolResult("Room activity accepted.")

                    class ReceiptCaptureHook(AgentHook):
                        def __init__(self) -> None:
                            super().__init__()
                            self.receipts: list[dict[str, Any]] = []
                            self.bytes = 0

                        async def after_execute_tool(
                            self,
                            _context: Any,
                            tool_call: Any,
                            _tool: Any,
                            _params: Any,
                            result: Any,
                        ) -> None:
                            try:
                                structured = normalize_resolution_presentation(
                                    getattr(result, "structured_content", None)
                                )
                            except ValueError:
                                return
                            if structured is None or len(self.receipts) >= 32:
                                return
                            try:
                                size = len(
                                    json.dumps(structured, ensure_ascii=False).encode("utf-8")
                                )
                            except (TypeError, ValueError):
                                return
                            if size > 131_072 or self.bytes + size > 262_144:
                                return
                            self.bytes += size
                            self.receipts.append(
                                {
                                    "tool": str(getattr(tool_call, "name", ""))[:160],
                                    "structured_content": structured,
                                }
                            )

                    registered_tools = await agent_loop._tools_for_session(
                        f"service:{payload.session_id}"
                    )
                    structured_tool = StructuredOutputTool(
                        name=str(terminal_contract["name"]),
                        description=str(terminal_contract["description"]),
                        parameters=dict(terminal_contract["parameters"]),
                    )
                    if registered_tools.has(structured_tool.name):
                        raise ValueError("structured response tool name is already registered")
                    registered_tools.register(structured_tool)
                    if activity_contract is not None:
                        if not callback.get("url") or not callback.get("token"):
                            raise ValueError("activity callback is incomplete")
                        activity_tool = RoomActivityTool()
                        if registered_tools.has(activity_tool.name):
                            raise ValueError("activity tool name is already registered")
                        registered_tools.register(activity_tool)
                    turn_tools = registered_tools
                    receipt_hook = ReceiptCaptureHook()
                except (KeyError, TypeError, ValueError) as exc:
                    raise HTTPException(
                        status.HTTP_422_UNPROCESSABLE_CONTENT,
                        "invalid structured response contract",
                    ) from exc
            try:
                response = await agent_loop.process_direct(
                    content=content,
                    session_key=f"service:{payload.session_id}",
                    channel=principal_parts[0],
                    sender_id=principal_parts[1],
                    tools=turn_tools,
                    hooks=[receipt_hook] if receipt_hook is not None else None,
                )
                structured_output = (
                    structured_tool.submission if structured_tool is not None else None
                )
                tool_receipts = receipt_hook.receipts if receipt_hook is not None else []
            finally:
                if registered_tools is not None and structured_tool is not None:
                    registered_tools.unregister(structured_tool.name)
                if registered_tools is not None and activity_tool is not None:
                    registered_tools.unregister(activity_tool.name)
        usage = getattr(agent_loop, "_last_usage", None) or {}
        response_text = str(getattr(response, "content", response) or "")
        if structured_tool is not None and structured_output is None:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                "agent did not submit the required structured response",
            )
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
            "structured_output": structured_output,
            "tool_receipts": tool_receipts,
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
