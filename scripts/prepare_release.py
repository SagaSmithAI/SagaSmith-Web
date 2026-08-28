"""Validate a SagaSmith Web release candidate and write its immutable manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE_FILES = (
    ".github/workflows/release.yml",
    "Dockerfile",
    "component-versions.json",
    "compose.yaml",
    "docker-entrypoint.sh",
    "infrastructure/agent-supervisor-requirements.txt",
    "pyproject.toml",
    "uv.lock",
)


def _git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=ROOT, text=True, encoding="utf-8"
    ).strip()


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(
    *, tag: str, require_main: bool, require_clean: bool = True
) -> dict[str, object]:
    expected_tag = f"v{project_version()}"
    if tag != expected_tag:
        raise ValueError(f"release tag {tag!r} must match project version {expected_tag!r}")

    revision = _git("rev-parse", "HEAD")
    if require_main:
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", revision, "origin/main"],
            cwd=ROOT,
            check=True,
        )
    if require_clean and _git("status", "--porcelain"):
        raise ValueError("release candidate worktree must be clean")

    files = []
    for relative in RELEASE_FILES:
        path = ROOT / relative
        if not path.is_file():
            raise ValueError(f"required release input is missing: {relative}")
        files.append({"path": relative, "sha256": sha256(path), "size": path.stat().st_size})

    component_lock = json.loads((ROOT / "component-versions.json").read_text(encoding="utf-8"))
    return {
        "schema": "sagasmith.web-release/v1",
        "product": "SagaSmith Web",
        "version": project_version(),
        "tag": tag,
        "revision": revision,
        "source_date": _git("show", "-s", "--format=%cI", "HEAD"),
        "component_lock_schema": component_lock["schema"],
        "component_product_scope": component_lock["product_scope"],
        "inputs": files,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-main", action="store_true")
    args = parser.parse_args()

    try:
        manifest = build_manifest(tag=args.tag, require_main=args.require_main)
    except (KeyError, OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"release candidate rejected: {exc}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
