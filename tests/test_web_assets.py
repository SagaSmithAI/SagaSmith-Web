from __future__ import annotations

import re

from fastapi.testclient import TestClient

_STATIC_IMPORT = re.compile(r'from\s+"(/assets/[^"]+)"')


def test_browser_entry_loads_complete_precached_module_graph(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert '<script type="module" src="/app.js"></script>' in page.text

    service_worker = client.get("/service-worker.js")
    assert service_worker.status_code == 200
    assert 'const CACHE="sagasmith-shell-v11"' in service_worker.text

    expected_modules = {
        "/assets/api/client.js",
        "/assets/auth/controller.js",
        "/assets/campaign/controller.js",
        "/assets/components/dom.js",
        "/assets/components/pwa.js",
        "/assets/components/toast.js",
        "/assets/forge/catalog.js",
        "/assets/forge/controller.js",
        "/assets/forge/moderation.js",
        "/assets/forge/shared.js",
        "/assets/forge/studio.js",
        "/assets/identity/controller.js",
        "/assets/module-studio/controller.js",
        "/assets/room/characters.js",
        "/assets/room/combat-grid.js",
        "/assets/room/combat-grid-state.js",
        "/assets/room/encounter-planner-state.js",
        "/assets/room/controller.js",
        "/assets/room/model.js",
        "/assets/room/timeline.js",
        "/assets/room/view.js",
        "/assets/state/store.js",
    }
    pending = ["/app.js"]
    visited: set[str] = set()
    discovered: set[str] = set()
    import_graph: dict[str, set[str]] = {}
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        response = client.get(path)
        assert response.status_code == 200, path
        media_type = response.headers["content-type"].partition(";")[0]
        assert media_type in {"application/javascript", "text/javascript"}, path
        imports = set(_STATIC_IMPORT.findall(response.text))
        import_graph[path] = imports
        discovered.update(imports)
        pending.extend(imports - visited)

    assert discovered == expected_modules
    for path in expected_modules:
        assert f'"{path}"' in service_worker.text
    texture = client.get("/sagasmith-grid-texture.webp")
    assert texture.status_code == 200
    assert texture.headers["content-type"] == "image/webp"
    assert len(texture.content) < 250_000
    assert '"/sagasmith-grid-texture.webp"' in service_worker.text

    entry = client.get("/app.js").text
    assert "/api/community" not in entry
    assert "/api/identities" not in entry
    assert "/api/campaigns/" not in entry
    assert "#character-select" not in entry
    assert "#combat-grid" not in entry
    forge_sources = "".join(
        client.get(path).text for path in expected_modules if path.startswith("/assets/forge/")
    )
    identity_source = client.get("/assets/identity/controller.js").text
    assert "/assets/identity/" not in forge_sources
    assert "/assets/forge/" not in identity_source

    room_sources = "".join(
        client.get(path).text for path in expected_modules if path.startswith("/assets/room/")
    )
    for feature in ("campaign", "forge", "identity", "module-studio"):
        assert f'/assets/{feature}/' not in room_sources
    room_controller = client.get("/assets/room/controller.js").text
    assert "panelRefreshPromise" in room_controller
    assert "known_revision=" in room_controller
    assert "sagasmith:panel-refresh" in room_controller
    assert 'recordPanelRefresh("coalesced")' in room_controller
    assert "}, 60000);" in entry

    visiting: set[str] = set()
    complete: set[str] = set()

    def visit(path: str) -> None:
        assert path not in visiting, f"circular browser import at {path}"
        if path in complete:
            return
        visiting.add(path)
        for imported in import_graph.get(path, set()):
            visit(imported)
        visiting.remove(path)
        complete.add(path)

    visit("/app.js")


def test_asset_namespace_does_not_capture_backend_api(client: TestClient) -> None:
    asset = client.get("/assets/api/client.js")
    health = client.get("/api/health")

    assert asset.status_code == 200
    assert "export async function api" in asset.text
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
