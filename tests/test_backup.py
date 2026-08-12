import hashlib
import json
from pathlib import Path

import pytest

from sagasmith_service.backup import REQUIRED_FILES, verify_backup


def build_backup(root: Path) -> None:
    entries = []
    for name in REQUIRED_FILES:
        content = f"backup:{name}".encode()
        (root / name).write_bytes(content)
        entries.append(
            {
                "name": name,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "consistency": "application-writers-stopped",
                "service_release": "test-release",
                "created_at": "2026-08-13T00:00:00Z",
                "files": entries,
            }
        ),
        encoding="utf-8",
    )


def test_backup_verifier_accepts_complete_checksum_manifest(tmp_path: Path) -> None:
    build_backup(tmp_path)
    assert verify_backup(tmp_path)["service_release"] == "test-release"


def test_backup_verifier_rejects_tampering_and_unsafe_paths(tmp_path: Path) -> None:
    build_backup(tmp_path)
    (tmp_path / "dnd-state.tgz").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="backup size mismatch"):
        verify_backup(tmp_path)

    build_backup(tmp_path)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    manifest["files"][0]["name"] = "../outside"
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="missing"):
        verify_backup(tmp_path)
