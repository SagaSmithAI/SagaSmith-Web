from __future__ import annotations

from types import SimpleNamespace

from conftest import FakeAgentRuntime, FakeDndRuntime
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.dialects import postgresql
from test_packs import pack_archive

from sagasmith_service.api.community import _artifact_catalog_statement, _artifact_search_filter
from sagasmith_service.models import (
    Artifact,
    ArtifactCollaborator,
    ArtifactFavorite,
    ArtifactRelease,
    CommunityPost,
    now_utc,
)

PASSWORD = "correct horse battery staple"


def test_postgresql_artifact_search_matches_the_indexed_document() -> None:
    session = SimpleNamespace(
        bind=SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    )
    statement = _artifact_catalog_statement().where(
        _artifact_search_filter(session, "dragon gate")
    )
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert (
        "to_tsvector('simple'::regconfig, coalesce(artifacts.title, '') || ' ' || "
        "coalesce(artifacts.summary, ''))"
    ) in compiled
    assert "websearch_to_tsquery('simple'::regconfig" in compiled


def register(client: TestClient, email: str, name: str) -> dict:
    response = client.post(
        "/api/auth/register",
        json={"email": email, "password": PASSWORD, "display_name": name},
    )
    assert response.status_code == 201, response.text
    return response.json()["user"]


def login(client: TestClient, email: str) -> None:
    response = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text


def publish(
    client: TestClient,
    agent_runtime: FakeAgentRuntime,
    *,
    artifact_type: str,
    slug: str,
    title: str,
    private_pack_id: str | None = None,
    payload: dict | None = None,
    system_id: str = "dnd5e",
) -> tuple[dict, dict]:
    artifact = client.post(
        "/api/community/artifacts",
        json={
            "slug": slug,
            "artifact_type": artifact_type,
            "title": title,
            "summary": f"Public {title}",
            "system_id": system_id,
            "visibility": "public",
            "license_code": "CC-BY-4.0",
            "rights_attested": True,
            "source_kind": "original",
            "provenance": {"author_statement": "Original work"},
            "tags": [artifact_type, system_id],
        },
    )
    assert artifact.status_code == 201, artifact.text
    release = client.post(
        f"/api/community/artifacts/{artifact.json()['id']}/releases",
        json={
            "version": "1.0.0",
            "manifest": {"format": f"sagasmith.{artifact_type}"},
            "payload": payload or {},
            "compatibility": {"edition": "2024"},
            "private_pack_id": private_pack_id,
        },
    )
    assert release.status_code == 201, release.text
    agent_runtime.content = (
        '{"approved":true,"summary":"Original and structurally complete","findings":[]}'
    )
    reviewed = client.post(
        f"/api/community/artifacts/{artifact.json()['id']}/releases/"
        f"{release.json()['id']}/agent-review",
        headers={"Idempotency-Key": f"review-{slug}-1000"},
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["status"] == "agent_reviewed"
    submitted = client.post(
        f"/api/community/artifacts/{artifact.json()['id']}/releases/{release.json()['id']}/submit"
    )
    assert submitted.status_code == 200, submitted.text
    login(client, "admin@forge.example.com")
    moderated = client.post(
        f"/api/community/admin/releases/{release.json()['id']}/moderate",
        json={"decision": "approved", "notes": "Reviewed for publication"},
    )
    assert moderated.status_code == 200, moderated.text
    return artifact.json(), moderated.json()


def test_public_module_catalog_discussion_fork_install_and_report(
    client: TestClient,
    dnd_runtime: FakeDndRuntime,
    agent_runtime: FakeAgentRuntime,
) -> None:
    client.app.state.settings.bootstrap_admin_email = "admin@forge.example.com"
    register(client, "admin@forge.example.com", "Moderator")
    author = register(client, "author@forge.example.com", "Author")
    uploaded = client.post(
        "/api/packs",
        data={
            "pack_id": "open-module",
            "version": "1.0.0",
            "title": "Open Module Payload",
            "kind": "module",
            "rights_attested": "true",
        },
        files={
            "archive": (
                "open-module.sagasmith-pack",
                pack_archive(pack_id="open-module"),
                "application/vnd.sagasmith.content-package+zip",
            )
        },
    )
    assert uploaded.status_code == 201, uploaded.text
    artifact, release = publish(
        client,
        agent_runtime,
        artifact_type="module",
        slug="open-module",
        title="Open Module",
        private_pack_id=uploaded.json()["id"],
    )

    reader = register(client, "reader@forge.example.com", "Reader DM")
    catalog = client.get("/api/community/artifacts", params={"q": "Open Module"})
    assert [item["id"] for item in catalog.json()] == [artifact["id"]]
    assert client.put(f"/api/community/artifacts/{artifact['id']}/favorite").status_code == 204
    post = client.post(
        "/api/community/posts",
        json={
            "target_type": "artifact",
            "target_id": artifact["id"],
            "release_id": release["id"],
            "category": "play_report",
            "spoiler": True,
            "body": "The sealed door worked well in play.",
        },
    )
    assert post.status_code == 201, post.text
    assert (
        client.get(
            "/api/community/posts",
            params={"target_type": "artifact", "target_id": artifact["id"]},
        ).json()[0]["spoiler"]
        is True
    )
    forked = client.post(
        f"/api/community/artifacts/{artifact['id']}/fork",
        json={"slug": "reader-remix", "title": "Reader Remix"},
    )
    assert forked.status_code == 201, forked.text
    assert forked.json()["forked_from_artifact_id"] == artifact["id"]

    campaign = client.post(
        "/api/campaigns",
        headers={"Idempotency-Key": "community-reader-campaign"},
        json={"name": "Community Campaign"},
    )
    assert campaign.status_code == 201, campaign.text
    installed = client.post(
        f"/api/community/releases/{release['id']}/install",
        headers={"Idempotency-Key": "install-community-module"},
        json={"campaign_id": campaign.json()["id"], "activate": True},
    )
    assert installed.status_code == 201, installed.text
    assert installed.json()["status"] == "activated"
    repeated_install = client.post(
        f"/api/community/releases/{release['id']}/install",
        headers={"Idempotency-Key": "install-community-module-retry"},
        json={"campaign_id": campaign.json()["id"], "activate": True},
    )
    assert repeated_install.status_code == 201
    assert repeated_install.json()["id"] == installed.json()["id"]
    import_call = [call for call in dnd_runtime.calls if call[0] == "content_pack_import"][-1]
    assert import_call[1]["principal_id"] == f"user:{reader['id']}"
    assert len([call for call in dnd_runtime.calls if call[0] == "content_pack_import"]) == 1

    login(client, "author@forge.example.com")
    frozen = client.patch(
        f"/api/community/artifacts/{artifact['id']}",
        json={"source_kind": "private_source", "visibility": "private"},
    )
    assert frozen.status_code == 409
    rereview = client.post(
        f"/api/community/artifacts/{artifact['id']}/releases/{release['id']}/agent-review",
        headers={"Idempotency-Key": "review-published-again"},
    )
    assert rereview.status_code == 409
    login(client, "reader@forge.example.com")

    report = client.post(
        "/api/community/reports",
        json={
            "target_type": "artifact",
            "target_id": artifact["id"],
            "reason": "copyright",
            "details": "Please verify the source declaration.",
        },
    )
    assert report.status_code == 201
    assert client.post(
        "/api/community/reports",
        json={
            "target_type": "artifact",
            "target_id": "00000000-0000-0000-0000-000000000000",
            "reason": "other",
        },
    ).status_code == 404
    login(client, "admin@forge.example.com")
    resolved = client.post(
        f"/api/community/admin/reports/{report.json()['id']}/decision",
        json={"status": "resolved", "resolution": "Withdrawn while rights are reviewed"},
    )
    assert resolved.status_code == 200
    login(client, "reader@forge.example.com")
    assert artifact["id"] not in {
        item["id"] for item in client.get("/api/community/artifacts").json()
    }
    assert author["id"] != reader["id"]


def test_private_source_and_executable_rule_cannot_enter_publication(
    client: TestClient,
) -> None:
    register(client, "safety@forge.example.com", "Safety Author")
    private_source = client.post(
        "/api/community/artifacts",
        json={
            "slug": "commercial-notes",
            "artifact_type": "soul",
            "title": "Commercial Notes",
            "visibility": "public",
            "rights_attested": True,
            "source_kind": "private_source",
        },
    )
    assert private_source.status_code == 422

    rule_pack = client.post(
        "/api/packs",
        data={
            "pack_id": "unsafe-rule",
            "version": "1.0.0",
            "title": "Unsafe Rule",
            "kind": "addon",
            "rights_attested": "true",
        },
        files={
            "archive": (
                "unsafe-rule.sagasmith-pack",
                pack_archive(pack_id="unsafe-rule", kind="addon"),
                "application/zip",
            )
        },
    )
    rule = client.post(
        "/api/community/artifacts",
        json={
            "slug": "unsafe-rule",
            "artifact_type": "rule",
            "title": "Unsafe Rule",
            "visibility": "private",
            "rights_attested": True,
        },
    )
    rejected = client.post(
        f"/api/community/artifacts/{rule.json()['id']}/releases",
        json={
            "version": "1.0.0",
            "private_pack_id": rule_pack.json()["id"],
            "payload": {"executable": {"python": "do_not_run()"}},
        },
    )
    assert rejected.status_code == 422
    assert "executable" in rejected.json()["detail"]


def test_artifact_catalog_query_count_is_constant_and_paginates(client: TestClient) -> None:
    owner = register(client, "catalog-owner@forge.example.com", "Catalog Owner")
    with client.app.state.session_factory() as session:
        for index in range(25):
            artifact = Artifact(
                owner_user_id=owner["id"],
                slug=f"catalog-{index:02d}",
                artifact_type="module",
                title=f"Indexed Adventure {index:02d}",
                summary="Searchable catalog entry",
                visibility="public",
                status="published",
                rights_attested=True,
            )
            session.add(artifact)
            session.flush()
            release = ArtifactRelease(
                artifact_id=artifact.id,
                version=f"1.0.{index}",
                status="published",
                published_at=now_utc(),
            )
            session.add(release)
            if index % 2 == 0:
                session.add(ArtifactFavorite(artifact_id=artifact.id, user_id=owner["id"]))
        session.commit()

    statements: list[str] = []

    def record_statement(*args) -> None:
        statements.append(str(args[2]))

    event.listen(client.app.state.engine, "before_cursor_execute", record_statement)
    try:
        first_page = client.get(
            "/api/community/artifacts",
            params={"q": "Indexed Adventure", "limit": 10},
        )
        first_selects = sum(
            statement.lstrip().upper().startswith("SELECT")
            and any(
                table in statement.lower()
                for table in ("artifacts", "artifact_releases", "artifact_favorites")
            )
            for statement in statements
        )
        statements.clear()
        second_page = client.get(
            "/api/community/artifacts",
            params={"q": "Indexed Adventure", "limit": 10, "offset": 10},
        )
        second_selects = sum(
            statement.lstrip().upper().startswith("SELECT")
            and any(
                table in statement.lower()
                for table in ("artifacts", "artifact_releases", "artifact_favorites")
            )
            for statement in statements
        )
    finally:
        event.remove(client.app.state.engine, "before_cursor_execute", record_statement)

    assert first_page.status_code == second_page.status_code == 200
    assert len(first_page.json()) == len(second_page.json()) == 10
    assert {item["id"] for item in first_page.json()}.isdisjoint(
        item["id"] for item in second_page.json()
    )
    assert first_selects == second_selects
    assert first_selects == 1
    assert all(item["latest_release_id"] for item in first_page.json())
    favorites_page = client.get("/api/community/artifacts", params={"favorites": True})
    assert favorites_page.status_code == 200
    assert len(favorites_page.json()) == 13


def test_post_query_count_does_not_grow_with_owner_only_posts(client: TestClient) -> None:
    owner = register(client, "post-owner@forge.example.com", "Post Owner")
    viewer = register(client, "post-editor@forge.example.com", "Post Editor")
    with client.app.state.session_factory() as session:
        artifact = Artifact(
            owner_user_id=owner["id"],
            slug="owner-thread",
            artifact_type="module",
            title="Owner Thread",
            visibility="public",
            status="published",
            rights_attested=True,
        )
        session.add(artifact)
        session.flush()
        session.add(
            ArtifactCollaborator(
                artifact_id=artifact.id,
                user_id=viewer["id"],
                role="editor",
                status="active",
            )
        )
        for index in range(30):
            session.add(
                CommunityPost(
                    author_user_id=owner["id"],
                    target_type="artifact",
                    target_id=artifact.id,
                    audience="owners",
                    body=f"Owner note {index}",
                )
            )
        session.commit()
        artifact_id = artifact.id

    statements: list[str] = []

    def record_statement(*args) -> None:
        statements.append(str(args[2]))

    event.listen(client.app.state.engine, "before_cursor_execute", record_statement)
    try:
        response = client.get(
            "/api/community/posts",
            params={"target_type": "artifact", "target_id": artifact_id},
        )
    finally:
        event.remove(client.app.state.engine, "before_cursor_execute", record_statement)

    select_count = sum(
        statement.lstrip().upper().startswith("SELECT")
        and any(
            table in statement.lower()
            for table in ("artifacts", "artifact_collaborators", "community_posts")
        )
        for statement in statements
    )
    assert response.status_code == 200
    assert len(response.json()) == 30
    assert select_count <= 3
