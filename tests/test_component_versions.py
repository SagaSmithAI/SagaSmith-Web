import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_component_lock_covers_every_workspace_repository() -> None:
    lock = json.loads((ROOT / "component-versions.json").read_text(encoding="utf-8"))
    locked = {component["repository"] for component in lock["components"]}
    expected = {
        ".github",
        "SagaSmith-agent",
        "sagasmith-coc",
        "SagaSmith-coc-mcp",
        "SagaSmith-coc-skills",
        "sagasmith-coc-ui",
        "sagasmith-core",
        "sagasmith-dnd",
        "SagaSmith-dnd-content-library",
        "SagaSmith-dnd-mcp",
        "SagaSmith-dnd-skills",
        "sagasmith-dnd-ui",
        "SagaSmith-module-gen-skills",
        "SagaSmith-narrative-mcp",
        "SagaSmith-narrative-skills",
        "SagaSmith-service",
        "sagasmith-ui",
        "SagaSmithAI.github.io",
    }

    assert lock["schema"] == "sagasmith.component-lock/v1"
    assert locked == expected
    assert len({component["repository"] for component in lock["components"]}) == len(
        lock["components"]
    )


def test_compose_pins_every_enforced_component_revision() -> None:
    lock = json.loads((ROOT / "component-versions.json").read_text(encoding="utf-8"))
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    for component in lock["components"]:
        if component["enforced"]:
            assert f"#{component['revision']}" in compose


def test_hosted_agent_uses_current_session_scoped_native_tool_contract() -> None:
    paths = (
        ROOT / "config" / "agent-config.example.json",
        ROOT / "tests/e2e/agent-config.json",
    )
    for path in paths:
        config = json.loads(path.read_text(encoding="utf-8"))
        server = config["tools"]["mcpServers"]["sagasmith_dnd"]
        assert server["injectPrincipal"] is True
        assert server["sessionScoped"] is True
        assert server["exposeResourcesAndPrompts"] is True
        assert server["toolTimeout"] == 900
        assert server["enabledTools"] == ["*"]
        skills = config["agents"]["defaults"]["externalSkillsDirs"]
        assert skills == [
            "/opt/sagasmith/skills/dnd",
            "/opt/sagasmith/skills/modulegen",
        ]
