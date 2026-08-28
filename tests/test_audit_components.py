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


def _git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=ROOT, text=True, encoding="utf-8"
    ).strip()


def test_remote_audit_accepts_revision_in_declared_branch_history():
    component = {
        "repository": "SagaSmith-Web",
        "branch": _git("branch", "--show-current"),
        "revision": _git("rev-parse", "HEAD"),
        "remote": str(ROOT),
        "usage": "test",
        "enforced": True,
    }

    result = audit_components.inspect_remote_component(component)

    assert result["status"] == "ok"
    assert result["problems"] == []
    assert result["branch_head"] == component["revision"]


def test_remote_audit_rejects_unknown_revision():
    component = {
        "repository": "SagaSmith-Web",
        "branch": _git("branch", "--show-current"),
        "revision": "0" * 40,
        "remote": str(ROOT),
        "usage": "test",
        "enforced": True,
    }

    result = audit_components.inspect_remote_component(component)

    assert result["status"] == "drift"
    assert "not in the declared remote branch" in result["problems"][0]
