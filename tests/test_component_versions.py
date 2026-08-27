import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_component_lock_covers_every_current_repository() -> None:
    lock = json.loads((ROOT / "component-versions.json").read_text(encoding="utf-8"))
    locked = {component["repository"] for component in lock["components"]}
    expected = {
        ".github",
        "SagaSmith-agent",
        "sagasmith-coc",
        "sagasmith-core",
        "sagasmith-dnd",
        "SagaSmith-dnd-content-library",
        "sagasmith-narrative",
        "SagaSmith-service",
        "SagaSmithAI.github.io",
    }

    assert lock["schema"] == "sagasmith.component-lock/v1"
    assert locked == expected
    assert len({component["repository"] for component in lock["components"]}) == len(
        lock["components"]
    )


def test_deployment_defaults_pin_every_enforced_component_revision() -> None:
    lock = json.loads((ROOT / "component-versions.json").read_text(encoding="utf-8"))
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    variables = {
        "SagaSmith-agent": "SAGASMITH_AGENT_CONTEXT",
        "sagasmith-core": "SAGASMITH_CORE_CONTEXT",
        "sagasmith-dnd": "SAGASMITH_DND_CONTEXT",
        "sagasmith-coc": "SAGASMITH_COC_CONTEXT",
        "sagasmith-narrative": "SAGASMITH_NARRATIVE_CONTEXT",
    }
    for component in lock["components"]:
        if component["enforced"]:
            variable = variables[component["repository"]]
            pinned_remote = f"{component['remote']}#{component['revision']}"
            assert f"{variable}={pinned_remote}" in env_example
            assert f"${{{variable}:-{pinned_remote}}}" in compose


def test_service_release_manifest_pins_every_runtime_layer() -> None:
    lock = json.loads((ROOT / "component-versions.json").read_text(encoding="utf-8"))
    runtime_locks = lock["runtime_locks"]
    assert runtime_locks["service_api"] == "uv.lock"
    assert runtime_locks["agent_supervisor"] == (
        "infrastructure/agent-supervisor-requirements.txt"
    )
    assert runtime_locks["uv_version"] == "0.11.25"
    assert runtime_locks["dependency_cutoff"] == "2026-08-17T12:00:00Z"
    deployment_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / "Dockerfile",
            ROOT / "compose.yaml",
            ROOT / "compose.e2e.yaml",
            ROOT / "infrastructure" / "Dockerfile.agent-supervisor",
            ROOT / "infrastructure" / "Dockerfile.dnd-mcp",
            ROOT / "infrastructure" / "Dockerfile.coc-mcp",
        )
    )
    for digest in runtime_locks["container_images"].values():
        assert digest in deployment_text


def test_agent_supervisor_has_one_hash_locked_dependency_solution() -> None:
    requirements = (
        ROOT / "infrastructure" / "agent-supervisor-requirements.txt"
    ).read_text(encoding="utf-8")
    packages = re.findall(r"^([a-z0-9][a-z0-9._-]*)==", requirements, re.MULTILINE)
    assert packages
    assert len(packages) == len(set(packages))
    for required in ("fastapi", "httpx", "mcp", "pydantic"):
        assert required in packages
    dockerfile = (ROOT / "infrastructure" / "Dockerfile.agent-supervisor").read_text(
        encoding="utf-8"
    )
    assert "--require-hashes -r /tmp/requirements.txt" in dockerfile
    assert "uv export" not in dockerfile


def test_agent_supervisor_lock_does_not_disclose_local_paths() -> None:
    requirements = (
        ROOT / "infrastructure" / "agent-supervisor-requirements.txt"
    ).read_text(encoding="utf-8")
    assert "file://" not in requirements.casefold()
    assert "# via" not in requirements.casefold()
    assert re.search(r"(?<![a-z])[a-z]:[\\/]", requirements, re.IGNORECASE) is None
    assert re.search(r"\(/(?:home|users|tmp|var/tmp)/", requirements, re.IGNORECASE) is None


def test_hosted_contract_requires_dynamic_scoped_structured_tools() -> None:
    lock = json.loads((ROOT / "component-versions.json").read_text(encoding="utf-8"))
    assert lock["required_contracts"]["agent"] == [
        "sessionScoped",
        "tools/list_changed",
        "structured_output",
        "tool_receipts",
    ]
    assert lock["required_contracts"]["mcp_common_tools"] == [
        "exposure",
        "server_capabilities",
        "campaign_query",
        "game_phase",
        "skill_query",
    ]
    assert lock["required_contracts"]["network_mcp_tools"] == [
        "storage_status",
        "resolution_presentation",
    ]
    assert lock["required_contracts"]["narrative_transport"] == (
        "process-local-stdio-only"
    )


def test_hosted_agent_uses_current_session_scoped_native_tool_contract() -> None:
    paths = (
        ROOT / "config" / "agent-config.example.json",
        ROOT / "tests/e2e/agent-config.json",
    )
    for path in paths:
        config = json.loads(path.read_text(encoding="utf-8"))
        for server_name in ("sagasmith_dnd", "sagasmith_coc", "sagasmith_narrative"):
            server = config["tools"]["mcpServers"][server_name]
            assert server["injectPrincipal"] is True
            assert server["sessionScoped"] is True
            assert server["exposeResourcesAndPrompts"] is True
            assert server["toolTimeout"] == 900
            assert server["enabledTools"] == ["*"]
        skills = config["agents"]["defaults"]["externalSkillsDirs"]
        assert skills == [
            "/opt/sagasmith/skills/hosted",
            "/opt/sagasmith/skills/dnd/full/skills",
            "/opt/sagasmith/skills/dnd-module-generator",
            "/opt/sagasmith/skills/coc/full/skills",
            "/opt/sagasmith/skills/coc-module-generator",
            "/opt/sagasmith/skills/narrative",
        ]
        narrative = config["tools"]["mcpServers"]["sagasmith_narrative"]
        assert narrative["type"] == "stdio"
        assert narrative["command"] == "/build/sagasmith-narrative/.venv/bin/python"


def test_supervisor_image_preserves_shared_skill_references() -> None:
    dockerfile = (
        ROOT / "infrastructure" / "Dockerfile.agent-supervisor"
    ).read_text(encoding="utf-8")
    assert "COPY --from=dnd_domain ./skills/full /opt/sagasmith/skills/dnd/full" in dockerfile
    assert "COPY --from=coc_domain ./skills/full /opt/sagasmith/skills/coc/full" in dockerfile
    assert "/opt/sagasmith/skills/dnd/full/references/mcp-contract.md" in dockerfile
    assert "/opt/sagasmith/skills/coc/full/references/mcp-contract.md" in dockerfile
    assert "/opt/sagasmith/skills/narrative/narrative-project-generator/SKILL.md" in dockerfile
