"""Inspect the D&D candidate lock and installed artifacts without reading secrets."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def text_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def inspect(source_root: Path | None = None, wheels: Path | None = None) -> dict:
    lock = json.loads((ROOT / "component-versions.dnd-beta.json").read_text("utf-8"))
    failures = []
    files = {}
    for name, expected in lock["file_hashes"].items():
        actual = text_hash(ROOT / name)
        files[name] = actual
        if actual != expected:
            failures.append(f"locked file changed: {name}")
    sources = {}
    if source_root:
        for component in lock["components"]:
            path = source_root / component["repository"]
            revision = subprocess.check_output(
                ["git", "-C", str(path), "rev-parse", "HEAD"], text=True
            ).strip()
            dirty = subprocess.check_output(
                ["git", "-C", str(path), "status", "--porcelain"], text=True
            ).splitlines()
            sources[component["repository"]] = {"revision": revision, "dirty_files": dirty}
            if component["revision"] != "self" and revision != component["revision"]:
                failures.append(f"source revision mismatch: {component['repository']}")
            if dirty:
                failures.append(f"uncommitted source changes: {component['repository']}")
    installed = {}
    for name in ("sagasmith-service", "nanobot-ai", "sagasmith-core", "sagasmith-dnd",
                 "sagasmith-dnd-runtime", "sagasmith-dnd-mcp"):
        try:
            installed[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            installed[name] = None
    artifacts = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(wheels.glob("*.whl"))
    } if wheels else {}
    return {"lock": lock["lock"], "status": "mismatch" if failures else "ok",
            "failures": failures, "source_revisions": sources, "file_hashes": files,
            "installed_versions": installed, "wheel_sha256": artifacts,
            "live_acceptance": "not configured"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--wheels", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = inspect(args.source_root, args.wheels)
    output = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    print(output, end="")
    if result["failures"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
