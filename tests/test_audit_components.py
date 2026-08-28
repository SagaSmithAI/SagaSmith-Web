from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "audit_components", ROOT / "scripts" / "audit_components.py"
)
assert SPEC and SPEC.loader
audit_components = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit_components)


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=repo, text=True, encoding="utf-8"
    ).strip()


def _remote_fixture(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "remote"
    subprocess.run(["git", "init", "-b", "main", repo], check=True, capture_output=True)
    (repo / "README.md").write_text("release fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Release Test",
            "-c",
            "user.email=release-test@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo, _git(repo, "rev-parse", "HEAD")


def test_remote_audit_accepts_revision_in_declared_branch_history(tmp_path: Path):
    repo, revision = _remote_fixture(tmp_path)
    component = {
        "repository": "SagaSmith-Web",
        "branch": "main",
        "revision": revision,
        "remote": str(repo),
        "usage": "test",
        "enforced": True,
    }

    result = audit_components.inspect_remote_component(component)

    assert result["status"] == "ok"
    assert result["problems"] == []
    assert result["branch_head"] == component["revision"]


def test_remote_audit_rejects_unknown_revision(tmp_path: Path):
    repo, _ = _remote_fixture(tmp_path)
    component = {
        "repository": "SagaSmith-Web",
        "branch": "main",
        "revision": "0" * 40,
        "remote": str(repo),
        "usage": "test",
        "enforced": True,
    }

    result = audit_components.inspect_remote_component(component)

    assert result["status"] == "drift"
    assert "not in the declared remote branch" in result["problems"][0]
