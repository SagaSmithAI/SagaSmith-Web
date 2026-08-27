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
    assert 'const CACHE="sagasmith-shell-v5"' in service_worker.text

    expected_modules = {
        "/assets/api/client.js",
        "/assets/auth/controller.js",
        "/assets/campaign/controller.js",
        "/assets/components/dom.js",
        "/assets/components/pwa.js",
        "/assets/components/toast.js",
        "/assets/module-studio/controller.js",
        "/assets/state/store.js",
    }
    pending = ["/app.js"]
    visited: set[str] = set()
    discovered: set[str] = set()
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
        discovered.update(imports)
        pending.extend(imports - visited)

    assert discovered == expected_modules
    for path in expected_modules:
        assert f'"{path}"' in service_worker.text


def test_asset_namespace_does_not_capture_backend_api(client: TestClient) -> None:
    asset = client.get("/assets/api/client.js")
    health = client.get("/api/health")

    assert asset.status_code == 200
    assert "export async function api" in asset.text
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
