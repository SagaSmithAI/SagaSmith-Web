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
        "SagaSmith-Web",
        "SagaSmithAI.github.io",
    }

    assert lock["schema"] == "sagasmith.release-lock/v3"
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
    assert runtime_locks["agent_supervisor"] == ("infrastructure/agent-supervisor-requirements.txt")
    assert runtime_locks["uv_version"] == "0.11.25"
    assert runtime_locks["dependency_cutoff"] == "2026-08-29T10:00:00Z"
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
    requirements = (ROOT / "infrastructure" / "agent-supervisor-requirements.txt").read_text(
        encoding="utf-8"
    )
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
    requirements = (ROOT / "infrastructure" / "agent-supervisor-requirements.txt").read_text(
        encoding="utf-8"
    )
    assert "file://" not in requirements.casefold()
    assert "# via" not in requirements.casefold()
    assert re.search(r"(?<![a-z])[a-z]:[\\/]", requirements, re.IGNORECASE) is None
    assert re.search(r"\(/(?:home|users|tmp|var/tmp)/", requirements, re.IGNORECASE) is None


def test_hosted_contract_requires_dynamic_scoped_structured_tools() -> None:
    lock = json.loads((ROOT / "component-versions.json").read_text(encoding="utf-8"))
    assert lock["compatibility"] == {
        "mcp_protocol": "2026-07-28",
        "legacy_protocol": "fallback-only",
        "auth_context": "sagasmith.auth-context/v2",
        "authority": "sagasmith.authoritative-mcp/v2",
    }
    assert lock["required_contracts"]["release_lock"] == "sagasmith.release-lock/v3"
    assert lock["required_contracts"]["authority"] == "sagasmith.authoritative-mcp/v2"
    assert lock["required_contracts"]["hosted_boundary"] == "modern"
    assert lock["required_contracts"]["agent"] == [
        "per_request_authority",
        "structured_output",
        "tool_receipts",
        "io.modelcontextprotocol/tasks",
    ]
    assert lock["required_contracts"]["host_tool_projection"] == {
        "maximum_tools": 16,
        "ordering": "sorted-unique",
        "catalog": "authorization-scoped-deterministic",
        "enforcement": "host-projection-plus-call-time-mcp-authorization",
    }
    assert lock["required_contracts"]["mcp_common_tools"] == [
        "server_capabilities",
        "campaign_query",
        "game_phase",
        "skill_query",
    ]
    assert lock["required_contracts"]["network_mcp_tools"] == [
        "storage_status",
        "resolution_presentation",
    ]
    assert lock["required_contracts"]["narrative_transport"] == ("process-local-stdio-only")
    serialized = json.dumps(lock["required_contracts"])
    assert "sessionScoped" not in serialized
    assert "tools/list_changed" not in serialized


def test_release_lock_matches_final_coordinated_component_set() -> None:
    lock = json.loads((ROOT / "component-versions.json").read_text(encoding="utf-8"))
    revisions = {item["repository"]: item["revision"] for item in lock["components"]}
    expected = {
        ".github": "6ee83e94efda617c1e0f36d370f87150a4e0c7d5",
        "SagaSmith-agent": "056f295360bcfa56a9ade7c6c151e9aea447df41",
        "sagasmith-core": "eef98fcfcaa96d08c069708b33ee7717ba1625c3",
        "sagasmith-dnd": "587f66e0673b686a7d47d1ee266d8404ef221741",
        "sagasmith-coc": "515f6a7e3ba3c2a41fff7de2624ee19e4deb6190",
        "sagasmith-narrative": "3f3694401dace148684f7fab9adda5b12679dfa0",
        "SagaSmithAI.github.io": "a737915ddd8fa3a479b584f0609b19cf9b880b8e",
    }
    for repository, revision in expected.items():
        assert revisions[repository] == revision
    assert lock["shared"] == {"sagasmith-core": expected["sagasmith-core"]}
    assert lock["profiles"] == {
        "dnd": {"sagasmith-dnd": expected["sagasmith-dnd"]},
        "coc": {"sagasmith-coc": expected["sagasmith-coc"]},
        "narrative": {"sagasmith-narrative": expected["sagasmith-narrative"]},
    }


def test_hosted_agent_uses_owned_modern_request_scoped_tool_contract() -> None:
    paths = (
        ROOT / "config" / "agent-config.example.json",
        ROOT / "config" / "agent-config.regression.json",
        ROOT / "tests/e2e/agent-config.json",
    )
    for path in paths:
        config = json.loads(path.read_text(encoding="utf-8"))
        expected_systems = {
            "sagasmith-dnd-mcp": ["dnd5e"],
            "sagasmith-coc-mcp": ["coc7e"],
            "sagasmith-narrative-mcp": ["narrative"],
        }
        for server_name, system_ids in expected_systems.items():
            server = config["tools"]["mcpServers"][server_name]
            assert server["injectPrincipal"] is True
            assert server["exposeResourcesAndPrompts"] is True
            assert server["toolTimeout"] == 900
            assert server["taskTimeout"] == 900
            assert server["enabledTools"] == ["*"]
            assert server["delegationSecret"]
            assert server["targetService"] == server_name
            assert server["authorizationAudience"] == server_name
            assert server["systemIds"] == system_ids
            assert server["protocolMode"] == "2026-07-28"
            assert "authContextSecret" not in server
            assert "sessionScoped" not in server
        skills = config["agents"]["defaults"]["externalSkillsDirs"]
        assert skills == [
            "/opt/sagasmith/skills/hosted",
            "/opt/sagasmith/skills/dnd/full/skills",
            "/opt/sagasmith/skills/dnd-module-generator",
            "/opt/sagasmith/skills/coc/full/skills",
            "/opt/sagasmith/skills/coc-module-generator",
            "/opt/sagasmith/skills/narrative",
        ]
        narrative = config["tools"]["mcpServers"]["sagasmith-narrative-mcp"]
        assert narrative["type"] == "stdio"
        assert narrative["command"] == "/build/sagasmith-narrative/.venv/bin/python"


def test_supervisor_image_preserves_shared_skill_references() -> None:
    dockerfile = (ROOT / "infrastructure" / "Dockerfile.agent-supervisor").read_text(
        encoding="utf-8"
    )
    assert "COPY --from=dnd_domain ./skills/full /opt/sagasmith/skills/dnd/full" in dockerfile
    assert "COPY --from=coc_domain ./skills/full /opt/sagasmith/skills/coc/full" in dockerfile
    assert "/opt/sagasmith/skills/dnd/full/references/mcp-contract.md" in dockerfile
    assert "/opt/sagasmith/skills/coc/full/references/mcp-contract.md" in dockerfile
    assert "/opt/sagasmith/skills/narrative/narrative-project-generator/SKILL.md" in dockerfile
