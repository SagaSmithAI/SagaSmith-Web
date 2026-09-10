from __future__ import annotations

import os

import pytest
from playwright.sync_api import expect, sync_playwright
from test_account_lifecycle import _register
from test_account_lifecycle import live_web as _live_web

live_web = _live_web

pytestmark = pytest.mark.skipif(
    os.environ.get("SAGASMITH_BROWSER_TESTS") != "1",
    reason="set SAGASMITH_BROWSER_TESTS=1 to run Chromium regressions",
)


def test_timeline_batches_snapshot_and_preserves_order(live_web: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(service_workers="block")
        page.goto(live_web)
        result = page.evaluate("""async () => {
          const { state } = await import('/assets/state/store.js');
          const { createRoomTimelineController } = await import('/assets/room/timeline.js');
          state.user = { id: 'viewer' };
          state.campaign = { id: 'a' };
          const timeline = document.querySelector('#messages');
          let reads = 0;
          Object.defineProperty(timeline, 'scrollHeight', {
            configurable: true, get() { reads++; return 20000; }
          });
          const message = (sequence) => ({ id: `m${sequence}`, sequence, sender_type: 'user',
            sender_user_id: 'viewer', sender_display_name: 'Viewer', content: `Line ${sequence}`,
            created_at: '2026-01-01T00:00:00Z', message_type: 'chat', audience: 'public' });
          const messages = Array.from({length: 200}, (_, i) => message(i + 1));
          messages[0].structured_payload = {suggestions:[{
            text:'Choose', valid_for:{pending_choice_id:'choice-1'}
          }]};
          for (const [index, status] of [[1, 'pending'], [199, 'settled']]) {
            messages[index].structured_payload = {blocks:[{type:'resolution_ref',
              resolution_id:'roll-1', presentation:{thread_id:'roll-1', event_sequence:index,
                status, pending_choice:{id:'choice-1'}}}]};
          }
          const originalFetch = globalThis.fetch;
          globalThis.fetch = async () => Response.json({
            room: {id:'a'}, event_cursor: 200, messages
          });
          const controller = createRoomTimelineController({});
          const start = performance.now();
          await controller.loadRoomSnapshot();
          const elapsed = performance.now() - start;
          const snapshotReads = reads;
          const settledSuggestionDisabled = timeline.querySelector('.suggestion-chip').disabled;
          controller.updateMessage({...message(100), content: 'Updated'});
          controller.updateMessage(message(0));
          controller.updateMessage(message(201));
          const sequences = [...timeline.children].map(node => Number(node.dataset.sequence));
          globalThis.fetch = originalFetch;
          delete timeline.scrollHeight;
          const updated = timeline.querySelector('[data-message-id="m100"]').textContent;
          return {snapshotReads, elapsed, sequences, updated, settledSuggestionDisabled};
        }""")
        assert result["snapshotReads"] <= 2, result
        assert result["sequences"] == list(range(202))
        assert "Updated" in result["updated"]
        assert result["settledSuggestionDisabled"] is True
        print(
            f"200-message snapshot: {result['snapshotReads']} scrollHeight reads; "
            f"{result['elapsed']:.2f} ms"
        )
        browser.close()


def test_old_snapshot_and_event_source_cannot_mutate_new_room(live_web: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(service_workers="block")
        page.goto(live_web)
        result = page.evaluate("""async () => {
          const { state } = await import('/assets/state/store.js');
          const { createRoomTimelineController } = await import('/assets/room/timeline.js');
          state.campaign = {id:'a'};
          state.user = {id:'viewer'};
          let resolveFetch;
          globalThis.fetch = () => new Promise(resolve => { resolveFetch = resolve; });
          let effects = 0;
          const effect = () => { effects++; return Promise.resolve(); };
          const controller = createRoomTimelineController({refreshPanel:effect, loadUsage:effect,
            loadCampaignIdentities:effect, leaveRoom:effect});
          const pending = controller.loadRoomSnapshot();
          state.roomGeneration++;
          state.campaign = {id:'b'};
          state.room = {id:'b'};
          resolveFetch(Response.json({room:{id:'a'},event_cursor:99,messages:[]}));
          await pending;
          const sources = [];
          globalThis.EventSource = class {
            handlers = {};
            constructor() {sources.push(this);}
            close() {}
            addEventListener(name, handler) {this.handlers[name] = handler;}
          };
          controller.connectRoomEvents();
          const old = sources[0];
          // Reopening even the same campaign creates a new lifetime.
          state.roomGeneration++;
          controller.connectRoomEvents();
          document.querySelector('#room-sync').textContent = 'Current room';
          old.onopen(); old.onerror();
          for (const handler of Object.values(old.handlers)) handler({data:'{}',lastEventId:'500'});
          return {room:state.room.id, effects,
            sync:document.querySelector('#room-sync').textContent,
            cursor:state.roomEventCursor};
        }""")
        assert result == {"room": "b", "effects": 0, "sync": "Current room", "cursor": 0}
        browser.close()


def test_character_transient_failure_recovers_and_coalesces(live_web: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(service_workers="block")
        page.goto(live_web)
        result = page.evaluate("""async () => {
          const { state } = await import('/assets/state/store.js');
          const { createCharacterController } = await import('/assets/room/characters.js');
          state.campaign = {id:'a'};
          state.user = {id:'viewer'};
          state.membership = {role:'owner'};
          state.panel = {characters:[{id:'hero',name:'Hero',revision:1}]};
          const controller = createCharacterController({sendPanelAction(){},drawCombatGrid(){}});
          let calls = 0;
          globalThis.fetch = async () => {
            calls++;
            return calls === 1 ? Response.json({detail:'Temporarily unavailable'}, {status:503})
              : Response.json({actor:{id:'hero',name:'Hero',revision:1}});
          };
          await controller.refreshCharacterSidebar();
          const denied = state.characterDenied.has('hero');
          await Promise.all([
            controller.refreshCharacterSidebar(), controller.refreshCharacterSidebar()
          ]);
          const cached = state.characterCards.has('hero');
          state.characterCards.clear();
          let resolveFetch;
          globalThis.fetch = () => new Promise(resolve => {resolveFetch = resolve;});
          const pending = controller.refreshCharacterSidebar();
          state.roomGeneration++;
          state.characterCards = new Map();
          resolveFetch(Response.json({actor:{id:'hero',revision:1}}));
          await pending;
          return {calls, denied, cached, staleCached:state.characterCards.has('hero')};
        }""")
        assert result == {"calls": 2, "denied": False, "cached": True, "staleCached": False}
        browser.close()


def test_campaign_retry_keyboard_and_duplicate_submission(live_web: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(service_workers="block", viewport={"width": 390, "height": 844})
        counts = {"get": 0, "post": 0}

        def campaign_request(route) -> None:
            method = route.request.method.lower()
            counts[method] += 1
            if method == "get" and counts[method] == 1:
                route.fulfill(status=503, content_type="application/json", body='{"detail":"Busy"}')
            else:
                route.continue_()

        page.route("**/api/campaigns", campaign_request)
        _register(page, live_web)
        page.locator("#campaign-list").get_by_role("button", name="重试").click()
        expect(page.locator("#campaign-list")).to_contain_text("还没有战役")
        page.locator("#new-campaign").click()
        form = page.locator("#campaign-form")
        expect(form.locator("input[name=name]")).to_be_focused()
        form.get_by_label("名称", exact=True).fill("Keyboard Campaign")
        form.evaluate("form => { form.requestSubmit(); form.requestSubmit(); }")
        card = page.get_by_role("button", name="打开战役：Keyboard Campaign")
        expect(card).to_be_visible()
        assert counts["post"] == 1
        if screenshot_path := os.environ.get("SAGASMITH_BROWSER_SCREENSHOT"):
            page.screenshot(path=screenshot_path + ".mobile.png", full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        card.focus()
        card.press("Enter")
        expect(page.locator("#room-title")).to_have_text("Keyboard Campaign")
        expect(page.locator("#campaign-room")).not_to_have_attribute("aria-busy", "true")
        expect(page.locator("#dm-tools")).to_be_hidden()
        for width in (390, 768, 900, 1024, 1100, 1280, 1366, 1440):
            page.set_viewport_size({"width": width, "height": 900})
            for mode in ("table", "player", "director"):
                page.locator(f'button[data-room-mode="{mode}"]').click()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
                    width, mode
                )
        page.locator('button[data-room-mode="table"]').click()
        page.locator("#message-form textarea").fill("Unsent campaign draft")
        page.locator("#back-campaigns").click()
        card.click()
        expect(page.locator("#message-form textarea")).to_have_value("Unsent campaign draft")
        expect(page.locator("#campaign-room")).not_to_have_attribute("aria-busy", "true")
        page.set_viewport_size({"width": 1280, "height": 900})
        if screenshot_path:
            page.screenshot(path=screenshot_path + ".room.png", full_page=True)
        browser.close()


def test_failed_room_open_keeps_retry_when_sibling_snapshot_finishes(live_web: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(service_workers="block")
        page.goto(live_web)
        result = page.evaluate("""async () => {
          const { state } = await import('/assets/state/store.js');
          const { createRoomController } = await import('/assets/room/controller.js');
          state.user = {id:'viewer'};
          let resolveSnapshot;
          globalThis.fetch = async (url) => {
            if (url.includes('/snapshot')) {
              return new Promise(resolve => {resolveSnapshot = resolve;});
            }
            return Response.json({detail:'Members unavailable'}, {status:503});
          };
          const controller = createRoomController({});
          await controller.openCampaign({id:'a', name:'Room A', system_id:'dnd5e'});
          resolveSnapshot(Response.json({room:{id:'a'},event_cursor:99,messages:[]}));
          await new Promise(resolve => setTimeout(resolve, 0));
          return {retry:!!document.querySelector('#messages button'), room:state.room,
            busy:document.querySelector('#campaign-room').getAttribute('aria-busy')};
        }""")
        assert result == {"retry": True, "room": None, "busy": None}
        browser.close()


def test_installed_shell_reloads_offline_without_caching_api(live_web: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        page.goto(live_web)
        page.evaluate("navigator.serviceWorker.ready.then(() => true)")
        page.reload()
        page.wait_for_function("navigator.serviceWorker.controller !== null")
        cached = page.evaluate("""async () => {
          const names = await caches.keys();
          const requests = await (await caches.open(names.find(name =>
            name.startsWith('sagasmith-shell-')))).keys();
          return requests.map(request => new URL(request.url).pathname);
        }""")
        assert "/app.js" in cached
        assert "/assets/room/timeline.js" in cached
        assert not any(path.startswith("/api/") for path in cached)
        page.context.set_offline(True)
        page.reload(wait_until="domcontentloaded")
        expect(page.locator("#auth-form")).to_be_visible()
        page.get_by_role("button", name="注册", exact=True).click()
        expect(page.locator("#name-row")).to_be_visible()
        browser.close()
