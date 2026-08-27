"""Regenerate the Service-owned dependency lock for the Agent supervisor image."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "infrastructure" / "agent-supervisor-requirements.txt"
COMPONENT_LOCK = ROOT / "component-versions.json"


def _lock_policy() -> tuple[str, str]:
    manifest = json.loads(COMPONENT_LOCK.read_text(encoding="utf-8"))
    runtime_locks = manifest["runtime_locks"]
    return runtime_locks["uv_version"], runtime_locks["dependency_cutoff"]


def _compile_command(agent_project: Path) -> list[str]:
    uv_version, dependency_cutoff = _lock_policy()
    return [
        "uvx",
        "--from",
        f"uv=={uv_version}",
        "uv",
        "pip",
        "compile",
        str(ROOT / "pyproject.toml"),
        str(agent_project),
        "--extra",
        "api",
        "--extra",
        "hosted",
        "--python-version",
        "3.12",
        "--universal",
        "--generate-hashes",
        "--exclude-newer",
        dependency_cutoff,
        "--quiet",
        "--no-annotate",
        "--no-emit-package",
        "sagasmith-service",
        "--no-emit-package",
        "nanobot-ai",
        "--custom-compile-command",
        "uv run python scripts/lock_agent_supervisor.py",
        "--output-file",
        str(OUTPUT),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-root", type=Path, default=ROOT.parent / "SagaSmith-agent")
    args = parser.parse_args()
    agent_project = args.agent_root.resolve() / "pyproject.toml"
    if not agent_project.is_file():
        raise SystemExit(f"Agent pyproject is unavailable: {agent_project}")
    subprocess.run(_compile_command(agent_project), cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
