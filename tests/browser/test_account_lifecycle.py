from __future__ import annotations

import base64
import os
import socket
import threading
import time
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest
import uvicorn
from playwright.sync_api import Page, expect, sync_playwright

from sagasmith_service.config import Settings
from sagasmith_service.database import make_engine
from sagasmith_service.main import create_app

pytestmark = pytest.mark.skipif(
    os.environ.get("SAGASMITH_BROWSER_TESTS") != "1",
    reason="set SAGASMITH_BROWSER_TESTS=1 to run the Chromium product smoke",
)


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.fixture
def live_web(tmp_path, dnd_runtime: Any, agent_runtime: Any) -> Iterator[str]:
    port = _free_port()
    database_url = f"sqlite:///{(tmp_path / 'browser.db').as_posix()}"
    settings = Settings(
        env="test",
        database_url=database_url,
        public_origin=f"http://127.0.0.1:{port}",
        private_storage_dir=str(tmp_path / "private"),
        exchange_dir=str(tmp_path / "exchange"),
    )
    app = create_app(
        settings,
        make_engine(database_url),
        dnd_runtime,
        agent_runtime,
        coc_runtime=dnd_runtime,
        narrative_runtime=dnd_runtime,
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            with urllib.request.urlopen(f"{base_url}/api/health", timeout=0.2) as response:
                if response.status == 200:
                    break
        except OSError:
            time.sleep(0.05)
    else:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError("browser test server did not become healthy")
    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()


def _register(page: Page, base_url: str) -> None:
    page.goto(base_url)
    page.wait_for_load_state("networkidle")
    page.get_by_role("button", name="注册").click()
    form = page.locator("#auth-form")
    form.get_by_label("邮箱").fill("browser@example.com")
    form.get_by_label("显示名称").fill("Browser Keeper")
    form.get_by_label("密码", exact=True).fill("correct-horse-battery-staple")
    form.get_by_label("我已阅读并同意").check()
    form.get_by_role("button", name="进入 SagaSmith").click()
    page.locator("#app").wait_for(state="visible")


def test_account_lifecycle_in_a_real_browser(live_web: str) -> None:
    console_errors: list[str] = []

    def record_console_error(message) -> None:
        if message.type == "error":
            console_errors.append(message.text)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("console", record_console_error)

        _register(page, live_web)
        page.get_by_role("button", name="账户", exact=True).click()
        page.locator("#account-view").wait_for(state="visible")
        expect(page.locator("#account-email")).to_have_text("browser@example.com")
        assert page.get_by_text("当前会话").is_visible()
        if screenshot_path := os.environ.get("SAGASMITH_BROWSER_SCREENSHOT"):
            page.screenshot(path=screenshot_path, full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        account_bounds = page.locator("#account-view").bounding_box()
        assert account_bounds is not None
        assert account_bounds["x"] >= 0
        assert account_bounds["x"] + account_bounds["width"] <= 391
        page.set_viewport_size({"width": 1280, "height": 900})

        profile_form = page.locator("#account-profile-form")
        profile_form.get_by_label("显示名称").fill("Lantern Keeper")
        profile_form.get_by_role("button", name="保存名称").click()
        page.get_by_text("显示名称已更新").wait_for(state="visible")
        assert page.locator("#identity").inner_text() == "Lantern Keeper"

        password_form = page.locator("#account-password-form")
        password_form.get_by_label("当前密码").fill("correct-horse-battery-staple")
        password_form.get_by_label("新密码", exact=True).fill("another-secure-passphrase")
        password_form.get_by_label("再次输入新密码").fill("another-secure-passphrase")
        password_form.get_by_role("button", name="更换并退出其他会话").click()
        page.get_by_text("密码已更新，其他会话已退出").wait_for(state="visible")

        page.goto(f"{live_web}/legal/privacy.html")
        page.wait_for_load_state("networkidle")
        assert page.get_by_role("heading", name="隐私说明", exact=True).is_visible()
        page.goto(f"{live_web}/legal/terms.html")
        page.wait_for_load_state("networkidle")
        assert page.get_by_role("heading", name="使用条款", exact=True).is_visible()

        page.goto(live_web)
        page.wait_for_load_state("networkidle")
        page.get_by_role("button", name="账户", exact=True).click()
        page.locator("#account-deactivate-form").get_by_label("当前密码").fill(
            "another-secure-passphrase"
        )
        page.get_by_label("输入 DEACTIVATE 确认").fill("DEACTIVATE")
        page.get_by_role("button", name="停用账户").click()
        page.locator("#auth").wait_for(state="visible")

        browser.close()
    unexpected_console_errors = [
        error for error in console_errors if "401 (Unauthorized)" not in error
    ]
    assert unexpected_console_errors == []


def test_hosted_room_media_and_combat_grid_render_in_a_real_browser(
    live_web: str, agent_runtime: Any
) -> None:
    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUB"
        "AScY42YAAAAASUVORK5CYII="
    )
    agent_runtime.mcp_results = (
        {
            "content": [
                {"type": "text", "text": "Rendered combat grid."},
                {
                    "type": "image",
                    "data": base64.b64encode(image).decode("ascii"),
                    "mimeType": "image/png",
                },
            ],
            "structuredContent": {"revision": 7},
            "isError": False,
        },
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        _register(page, live_web)
        request = page.context.request
        common_headers = {"Origin": live_web}
        campaign_response = request.post(
            f"{live_web}/api/campaigns",
            headers={**common_headers, "Idempotency-Key": "browser-media-campaign"},
            data={"name": "Browser Media", "edition": "2024"},
        )
        assert campaign_response.status == 201, campaign_response.text()
        campaign_id = campaign_response.json()["id"]
        room_response = request.post(
            f"{live_web}/api/campaigns/{campaign_id}/room/messages",
            headers={**common_headers, "Idempotency-Key": "browser-media-turn"},
            data={"content": "Render the shared map.", "mode": "action"},
        )
        assert room_response.status == 200, room_response.text()
        media_url = room_response.json()["agent_message"]["structured_payload"]["media"][0][
            "url"
        ]
        combat_response = request.post(
            f"{live_web}/api/campaigns/{campaign_id}/room/panel/actions",
            headers={**common_headers, "Idempotency-Key": "browser-grid-start"},
            data={
                "action": "combat.start",
                "base_revision": 7,
                "payload": {
                    "participant_ids": ["actor-1"],
                    "participant_config": [
                        {"actor_id": "actor-1", "position": {"x": 1, "y": 1}}
                    ],
                    "positioning_mode": "grid",
                    "name": "Browser Grid",
                    "battle_map": {
                        "width_cells": 8,
                        "height_cells": 6,
                        "blocked_cells": [],
                        "difficult_cells": [],
                    },
                    "battle_map_override_reason": "browser acceptance fixture",
                },
            },
        )
        assert combat_response.status == 200, combat_response.text()

        page.reload(wait_until="networkidle")
        page.locator("article.campaign", has_text="Browser Media").click()
        page.locator("#campaign-room").wait_for(state="visible")
        room_image = page.locator('.message-media img[src="%s"]' % media_url)
        expect(room_image).to_be_visible()
        page.wait_for_function(
            """url => {
                const image = document.querySelector(`.message-media img[src="${url}"]`);
                return image && image.complete && image.naturalWidth > 0;
            }""",
            arg=media_url,
        )
        assert room_image.evaluate("element => element.naturalWidth") == 1
        page.get_by_role("button", name="战斗", exact=True).click()
        expect(page.locator("#combat-grid")).to_be_visible()
        page.wait_for_function(
            "() => document.querySelector('#combat-grid')?.width > 0",
        )
        assert page.locator("#combat-grid").evaluate("element => element.width > 0")
        browser.close()
