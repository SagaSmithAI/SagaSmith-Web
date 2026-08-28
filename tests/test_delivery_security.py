from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_dependabot_does_not_bypass_coordinated_release_locks() -> None:
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    updates = {entry["package-ecosystem"]: entry for entry in config["updates"]}

    assert "infrastructure/agent-supervisor-requirements.txt" in updates["pip"][
        "exclude-paths"
    ]
    assert set(updates["docker"]["exclude-paths"]) == {
        "Dockerfile",
        "infrastructure/Dockerfile.*",
    }


def test_public_proxy_does_not_expose_prometheus_metrics() -> None:
    caddyfile = (ROOT / "infrastructure" / "Caddyfile").read_text(encoding="utf-8")

    matcher = "@private_metrics path /metrics"
    denial = "respond @private_metrics 404"
    proxy = "reverse_proxy api:8080"

    assert matcher in caddyfile
    assert denial in caddyfile
    assert caddyfile.index(matcher) < caddyfile.index(denial) < caddyfile.index(proxy)


def test_current_docs_do_not_restore_the_historical_repository_url() -> None:
    current_docs = [
        ROOT / "AGENTS.md",
        ROOT / "README.md",
        ROOT / "docs" / "architecture.md",
        ROOT / "docs" / "async-database-hotpaths.md",
    ]

    for path in current_docs:
        assert "github.com/SagaSmithAI/SagaSmith-service" not in path.read_text(encoding="utf-8")
