from conftest import FakeDndRuntime
from fastapi.testclient import TestClient


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
                "private.sagapack",
                b"finalized private pack bytes",
                "application/vnd.sagasmith.content-package+zip",
            )
        },
    )
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["distribution"] == "private"
    assert uploaded.json()["size_bytes"] == len(b"finalized private pack bytes")
    listed = client.get("/api/packs")
    assert [item["id"] for item in listed.json()] == [uploaded.json()["id"]]

    imported = client.post(
        f"/api/packs/{uploaded.json()['id']}/campaigns/campaign-1/import"
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["status"] == "imported"
    call = next(item for item in dnd_runtime.calls if item[0] == "content_pack_import")
    assert call[1]["kind"] == "module"
    assert call[1]["campaign_id"] == "campaign-1"
    assert call[1]["source_path"].endswith(".sagapack")

    repeated = client.post(
        f"/api/packs/{uploaded.json()['id']}/campaigns/campaign-1/import"
    )
    assert repeated.status_code == 200
    assert len([item for item in dnd_runtime.calls if item[0] == "content_pack_import"]) == 1


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
        files={"archive": ("private.sagapack", b"bytes", "application/zip")},
    )
    assert response.status_code == 422
