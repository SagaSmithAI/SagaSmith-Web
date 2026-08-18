from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

REQUIRED_FILES = {
    "control.dump",
    "object-data.tgz",
    "dnd-state.tgz",
    "coc-state.tgz",
    "agent-workspace.tgz",
}


def verify_backup(path: Path) -> dict[str, Any]:
    root = path.resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("manifest.json is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported backup manifest version")
    if manifest.get("consistency") != "application-writers-stopped":
        raise ValueError("backup does not claim a consistent write boundary")
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise ValueError("backup manifest files must be an array")
    names = {str(item.get("name") or "") for item in entries if isinstance(item, dict)}
    missing = REQUIRED_FILES - names
    if missing:
        raise ValueError("backup manifest is missing: " + ", ".join(sorted(missing)))
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("backup manifest file entry must be an object")
        name = str(item.get("name") or "")
        candidate = (root / name).resolve()
        if root not in candidate.parents or not candidate.is_file():
            raise ValueError(f"backup file is missing or unsafe: {name}")
        if candidate.stat().st_size != int(item.get("size_bytes", -1)):
            raise ValueError(f"backup size mismatch: {name}")
        digest = hashlib.sha256()
        with candidate.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != item.get("sha256"):
            raise ValueError(f"backup checksum mismatch: {name}")
    return manifest
