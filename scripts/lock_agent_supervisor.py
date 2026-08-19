"""Regenerate the Service-owned dependency lock for the Agent supervisor image."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "infrastructure" / "agent-supervisor-requirements.txt"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-root", type=Path, default=ROOT.parent / "SagaSmith-agent")
    args = parser.parse_args()
    agent_project = args.agent_root.resolve() / "pyproject.toml"
    if not agent_project.is_file():
        raise SystemExit(f"Agent pyproject is unavailable: {agent_project}")
    subprocess.run(
        [
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
            "--no-emit-package",
            "sagasmith-service",
            "--no-emit-package",
            "nanobot-ai",
            "--custom-compile-command",
            "uv run python scripts/lock_agent_supervisor.py",
            "--output-file",
            str(OUTPUT),
        ],
        cwd=ROOT,
        check=True,
    )


if __name__ == "__main__":
    main()
