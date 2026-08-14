"""Audit sibling SagaSmith clones against the reviewed component lock."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

SERVICE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKSPACE = SERVICE_ROOT.parent
DEFAULT_LOCK = SERVICE_ROOT / "component-versions.json"


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def _normalize_remote(value: str) -> str:
    return value.removesuffix("/").removesuffix(".git").casefold()


def inspect_component(
    component: dict[str, Any], workspace: Path, *, fetch: bool
) -> dict[str, Any]:
    repo = workspace / component["repository"]
    result = {
        "repository": component["repository"],
        "usage": component["usage"],
        "enforced": bool(component["enforced"]),
        "expected_branch": component["branch"],
        "expected_revision": component["revision"],
        "status": "ok",
        "problems": [],
    }
    problems: list[str] = result["problems"]
    if not (repo / ".git").exists():
        result["status"] = "missing"
        problems.append("repository is not cloned")
        return result
    try:
        if fetch:
            _git(repo, "fetch", "--all", "--prune", "--tags")
        head = _git(repo, "rev-parse", "HEAD")
        branch = _git(repo, "branch", "--show-current")
        remote = _git(repo, "remote", "get-url", "origin")
        dirty = bool(_git(repo, "status", "--porcelain"))
        upstream = _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        ahead, behind = (
            int(value)
            for value in _git(
                repo, "rev-list", "--left-right", "--count", f"HEAD...{upstream}"
            ).split()
        )
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        result["status"] = "error"
        problems.append(str(exc))
        return result
    result.update(
        {
            "branch": branch,
            "revision": head,
            "remote": remote,
            "dirty": dirty,
            "upstream": upstream,
            "ahead": ahead,
            "behind": behind,
        }
    )
    if branch != component["branch"]:
        problems.append(f"branch is {branch!r}")
    if head != component["revision"]:
        problems.append(f"revision is {head}")
    if _normalize_remote(remote) != _normalize_remote(component["remote"]):
        problems.append(f"origin is {remote!r}")
    if dirty:
        problems.append("worktree is dirty")
    if ahead or behind:
        problems.append(f"upstream divergence ahead={ahead} behind={behind}")
    if problems:
        result["status"] = "drift"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--scope", choices=("all", "build"), default="all")
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    lock = json.loads(args.lock.read_text(encoding="utf-8"))
    components = lock["components"]
    if args.scope == "build":
        components = [component for component in components if component["enforced"]]
    rows = [
        inspect_component(component, args.workspace.resolve(), fetch=args.fetch)
        for component in components
    ]
    summary = {
        "schema": lock["schema"],
        "audited_at": lock["audited_at"],
        "workspace": str(args.workspace.resolve()),
        "checked": len(rows),
        "ok": sum(row["status"] == "ok" for row in rows),
        "drift": sum(row["status"] != "ok" for row in rows),
        "components": rows,
    }
    if args.as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            marker = "OK" if row["status"] == "ok" else "DRIFT"
            revision = str(row.get("revision") or row["expected_revision"])[0:12]
            print(f"{marker:5} {row['repository']:<34} {revision}  {row['usage']}")
            for problem in row["problems"]:
                print(f"      - {problem}")
        print(f"checked={summary['checked']} ok={summary['ok']} drift={summary['drift']}")
    if not args.strict:
        return 0
    return 1 if any(row["enforced"] and row["status"] != "ok" for row in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
