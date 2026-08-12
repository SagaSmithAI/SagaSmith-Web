import io
import json
import zipfile
from pathlib import Path

from conftest import FakeDndRuntime
from fastapi.testclient import TestClient

from sagasmith_service.storage import PrivateStorageError


def pack_archive(
    *, pack_id: str = "private-module", version: str = "1.0.0", kind: str = "module"
) -> bytes:
    output = io.BytesIO()
    descriptor = {
        "format": "sagasmith.content-package",
        "schema_version": 2,
        "kind": kind,
        "id": pack_id,
        "version": version,
    }
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("package.sagasmith.json", json.dumps(descriptor))
    return output.getvalue()


def setup_dm(client: TestClient) -> None:
    client.post(
        "/api/auth/register",
        json={
            "email": "packs@example.com",
            "password": "correct horse battery staple",
            "display_name": "Pack DM",
        },
    )
    response = client.post(
        "/api/campaigns",
        headers={"Idempotency-Key": "pack-campaign-1"},
        json={"name": "Pack Campaign"},
    )
    assert response.status_code == 201


def test_private_pack_upload_and_mcp_import(
    client: TestClient, dnd_runtime: FakeDndRuntime
) -> None:
    setup_dm(client)
    archive_bytes = pack_archive()
    uploaded = client.post(
        "/api/packs",
        data={
            "pack_id": "private-module",
            "version": "1.0.0",
            "title": "Private Module",
            "kind": "module",
            "rights_attested": "true",
        },
        files={
            "archive": (
                "private.sagasmith-pack",
                archive_bytes,
                "application/vnd.sagasmith.content-package+zip",
            )
        },
    )
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["distribution"] == "private"
    assert uploaded.json()["size_bytes"] == len(archive_bytes)
    listed = client.get("/api/packs")
    assert [item["id"] for item in listed.json()] == [uploaded.json()["id"]]

    imported = client.post(f"/api/packs/{uploaded.json()['id']}/campaigns/campaign-1/import")
    assert imported.status_code == 200, imported.text
    assert imported.json()["status"] == "imported"
    assert imported.json()["runtime_ref"] == "module-1"
    call = next(item for item in dnd_runtime.calls if item[0] == "content_pack_import")
    assert call[1]["kind"] == "module"
    assert call[1]["campaign_id"] == "campaign-1"
    assert call[1]["source_path"].endswith(".sagasmith-pack")
    assert not Path(call[1]["source_path"]).exists()

    repeated = client.post(f"/api/packs/{uploaded.json()['id']}/campaigns/campaign-1/import")
    assert repeated.status_code == 200
    assert len([item for item in dnd_runtime.calls if item[0] == "content_pack_import"]) == 1

    activated = client.post(
        f"/api/packs/{uploaded.json()['id']}/campaigns/campaign-1/activate",
        headers={"Idempotency-Key": "activate-private-module"},
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["status"] == "activated"
    activation = next(item for item in dnd_runtime.calls if item[0] == "content_pack_activate")
    assert activation[1]["runtime_ref"] == "module-1"

    repeated_activation = client.post(
        f"/api/packs/{uploaded.json()['id']}/campaigns/campaign-1/activate",
        headers={"Idempotency-Key": "activate-private-module"},
    )
    assert repeated_activation.status_code == 200
    assert len([item for item in dnd_runtime.calls if item[0] == "content_pack_activate"]) == 1


def test_pack_import_reports_private_storage_outage(client: TestClient) -> None:
    setup_dm(client)
    uploaded = client.post(
        "/api/packs",
        data={
            "pack_id": "unavailable-module",
            "version": "1.0.0",
            "title": "Unavailable Module",
            "kind": "module",
            "rights_attested": "true",
        },
        files={
            "archive": (
                "unavailable.sagasmith-pack",
                pack_archive(pack_id="unavailable-module"),
                "application/vnd.sagasmith.content-package+zip",
            )
        },
    )
    assert uploaded.status_code == 201

    def unavailable(*_args: object, **_kwargs: object) -> Path:
        raise PrivateStorageError("object storage unavailable")

    client.app.state.private_storage.materialize_for_runtime = unavailable
    imported = client.post(f"/api/packs/{uploaded.json()['id']}/campaigns/campaign-1/import")
    assert imported.status_code == 503
    assert imported.json()["detail"] == "private Pack storage is temporarily unavailable"


def test_pack_upload_requires_rights_attestation(client: TestClient) -> None:
    client.post(
        "/api/auth/register",
        json={
            "email": "rights@example.com",
            "password": "correct horse battery staple",
            "display_name": "Rights User",
        },
    )
    response = client.post(
        "/api/packs",
        data={
            "pack_id": "no-rights",
            "version": "1",
            "title": "No Rights",
            "kind": "module",
            "rights_attested": "false",
        },
        files={"archive": ("private.sagasmith-pack", pack_archive(), "application/zip")},
    )
    assert response.status_code == 422


def test_pack_upload_rejects_retired_extension_and_unsafe_archive(client: TestClient) -> None:
    client.post(
        "/api/auth/register",
        json={
            "email": "unsafe-pack@example.com",
            "password": "correct horse battery staple",
            "display_name": "Unsafe Pack",
        },
    )
    retired = client.post(
        "/api/packs",
        data={
            "pack_id": "private-module",
            "version": "1.0.0",
            "title": "Retired",
            "kind": "module",
            "rights_attested": "true",
        },
        files={"archive": ("retired.sagapack", pack_archive(), "application/zip")},
    )
    assert retired.status_code == 422

    unsafe_bytes = io.BytesIO()
    with zipfile.ZipFile(unsafe_bytes, "w") as archive:
        archive.writestr(
            "package.sagasmith.json",
            json.dumps(
                {
                    "format": "sagasmith.content-package",
                    "id": "private-module",
                    "version": "1.0.0",
                    "kind": "module",
                }
            ),
        )
        archive.writestr("../commercial.pdf", b"must never be extracted")
    unsafe = client.post(
        "/api/packs",
        data={
            "pack_id": "private-module",
            "version": "1.0.0",
            "title": "Unsafe",
            "kind": "module",
            "rights_attested": "true",
        },
        files={
            "archive": (
                "unsafe.sagasmith-pack",
                unsafe_bytes.getvalue(),
                "application/zip",
            )
        },
    )
    assert unsafe.status_code == 422
    assert "unsupported paths" in unsafe.json()["detail"]


def test_pack_form_must_match_archive_descriptor(client: TestClient) -> None:
    client.post(
        "/api/auth/register",
        json={
            "email": "mismatch-pack@example.com",
            "password": "correct horse battery staple",
            "display_name": "Mismatch Pack",
        },
    )
    response = client.post(
        "/api/packs",
        data={
            "pack_id": "claimed-id",
            "version": "1.0.0",
            "title": "Mismatch",
            "kind": "module",
            "rights_attested": "true",
        },
        files={
            "archive": (
                "mismatch.sagasmith-pack",
                pack_archive(pack_id="actual-id"),
                "application/zip",
            )
        },
    )
    assert response.status_code == 422
    assert "id" in response.json()["detail"]
