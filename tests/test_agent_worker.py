import sys
import types

from fastapi.testclient import TestClient

from sagasmith_service.agent_worker import create_worker_app


class FakeLoop:
    def __init__(self) -> None:
        self.calls = []
        self._last_usage = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
        self.registry = FakeRegistry()
        self.structured_submission = None
        self.structured_tool_results = []

    async def _connect_mcp(self) -> None:
        return None

    async def close_mcp(self) -> None:
        return None

    async def _tools_for_session(self, _session_key: str):
        return self.registry

    async def process_direct(self, **arguments):
        self.calls.append(arguments)
        if self.structured_submission is not None:
            tool = arguments["tools"].get("submit_room_turn")
            await tool.execute(**self.structured_submission)
        for structured_content in self.structured_tool_results:
            for hook in arguments.get("hooks") or []:
                await hook.after_execute_tool(
                    None,
                    types.SimpleNamespace(name="mcp_resolution"),
                    None,
                    None,
                    types.SimpleNamespace(structured_content=structured_content),
                )
        return type("Response", (), {"content": "ok"})()


class FakeRegistry:
    def __init__(self) -> None:
        self.tools = {}

    def has(self, name: str) -> bool:
        return name in self.tools

    def register(self, tool) -> None:
        self.tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self.tools.pop(name, None)

    def get(self, name: str):
        return self.tools.get(name)


def test_hosted_worker_injects_authenticated_principal_as_sender() -> None:
    loop = FakeLoop()
    with TestClient(create_worker_app(loop, "test-model")) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "session_id": "campaign:user:conversation",
                "principal_id": "user:account-id",
            },
        )
    assert response.status_code == 200
    assert loop.calls[0]["channel"] == "user"
    assert loop.calls[0]["sender_id"] == "account-id"
    assert response.json()["usage"]["total_tokens"] == 5


def test_hosted_worker_rejects_untrusted_principal_shape() -> None:
    loop = FakeLoop()
    with TestClient(create_worker_app(loop, "test-model")) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "hello"}],
                "session_id": "conversation",
                "principal_id": "service:spoofed",
            },
        )
    assert response.status_code == 422


def test_hosted_worker_injects_agent_identity_principal() -> None:
    loop = FakeLoop()
    with TestClient(create_worker_app(loop, "test-model")) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "host this scene"}],
                "session_id": "campaign:agent:identity:conversation",
                "principal_id": "agent:identity-id",
            },
        )
    assert response.status_code == 200
    assert loop.calls[0]["channel"] == "agent"
    assert loop.calls[0]["sender_id"] == "identity-id"


def test_hosted_worker_captures_and_removes_required_structured_output_tool(
    monkeypatch,
) -> None:
    class FakeAgentHook:
        def __init__(self) -> None:
            pass

    class FakeStructuredOutputTool:
        def __init__(self, *, name, description, parameters) -> None:
            self.name = name
            self.description = description
            self.parameters = parameters
            self.submission = None

        async def execute(self, **kwargs):
            self.submission = kwargs

    nanobot = types.ModuleType("nanobot")
    agent = types.ModuleType("nanobot.agent")
    hook = types.ModuleType("nanobot.agent.hook")
    tools = types.ModuleType("nanobot.agent.tools")
    structured = types.ModuleType("nanobot.agent.tools.structured_output")
    resolution = types.ModuleType("nanobot.agent.resolution_presentation")
    hook.AgentHook = FakeAgentHook
    resolution.normalize_resolution_presentation = lambda value: (
        dict(value)
        if isinstance(value, dict)
        and value.get("schema") == "sagasmith.resolution-presentation/v1"
        else None
    )
    structured.StructuredOutputTool = FakeStructuredOutputTool
    nanobot.agent = agent
    agent.hook = hook
    agent.resolution_presentation = resolution
    agent.tools = tools
    tools.structured_output = structured
    monkeypatch.setitem(sys.modules, "nanobot", nanobot)
    monkeypatch.setitem(sys.modules, "nanobot.agent", agent)
    monkeypatch.setitem(sys.modules, "nanobot.agent.hook", hook)
    monkeypatch.setitem(sys.modules, "nanobot.agent.resolution_presentation", resolution)
    monkeypatch.setitem(sys.modules, "nanobot.agent.tools", tools)
    monkeypatch.setitem(sys.modules, "nanobot.agent.tools.structured_output", structured)

    loop = FakeLoop()
    loop.structured_submission = {"schema": "test/v1", "messages": []}
    loop.structured_tool_results = [
        {"private": "raw MCP receipt"},
        {
            "schema": "sagasmith.resolution-presentation/v1",
            "resolution_id": "resolution-1",
        },
    ]
    with TestClient(create_worker_app(loop, "test-model")) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "host this turn"}],
                "session_id": "campaign:user:conversation",
                "principal_id": "user:account-id",
                "response_contract": {
                    "name": "submit_room_turn",
                    "description": "Submit.",
                    "parameters": {"type": "object"},
                },
            },
        )

    assert response.status_code == 200, response.text
    assert response.json()["structured_output"] == loop.structured_submission
    assert response.json()["tool_receipts"] == [
        {
            "tool": "mcp_resolution",
            "structured_content": {
                "schema": "sagasmith.resolution-presentation/v1",
                "resolution_id": "resolution-1",
            },
        }
    ]
    assert loop.registry.get("submit_room_turn") is None
