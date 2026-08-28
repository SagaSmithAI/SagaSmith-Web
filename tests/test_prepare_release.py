from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "prepare_release", ROOT / "scripts" / "prepare_release.py"
)
assert SPEC and SPEC.loader
prepare_release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare_release)


def test_release_manifest_covers_immutable_inputs():
    manifest = prepare_release.build_manifest(
        tag="v0.1.0", require_main=False, require_clean=False
    )

    assert manifest["schema"] == "sagasmith.web-release/v1"
    assert manifest["version"] == "0.1.0"
    assert manifest["tag"] == "v0.1.0"
    assert manifest["revision"]
    assert {entry["path"] for entry in manifest["inputs"]} == set(
        prepare_release.RELEASE_FILES
    )
    assert all(len(entry["sha256"]) == 64 for entry in manifest["inputs"])
    json.dumps(manifest)


def test_release_manifest_rejects_version_mismatch():
    with pytest.raises(ValueError, match="must match project version"):
        prepare_release.build_manifest(tag="v9.9.9", require_main=False, require_clean=False)


def test_release_workflow_publishes_only_immutable_evidence():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert 'tags: ["v*"]' in workflow
    assert "--require-main" in workflow
    assert "audit_components.py --remote --scope build --strict" in workflow
    assert "--provenance=mode=max" in workflow
    assert "--sbom=true" in workflow
    assert 'docker push "${IMAGE,,}:latest"' not in workflow
    assert "release/release-manifest.json" in workflow
    assert "release/SHA256SUMS" in workflow
