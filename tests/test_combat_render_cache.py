from __future__ import annotations

import asyncio
import hashlib

from sagasmith_service.combat_render_cache import CombatRenderCache, CombatRenderKey
from sagasmith_service.integrations.dnd_mcp import DndCombatRender


def _render(content: bytes) -> DndCombatRender:
    return DndCombatRender(
        metadata={
            "audience_projection": "party_public",
            "image_checksum": hashlib.sha256(content).hexdigest(),
        },
        content=content,
        media_type="image/png",
    )


def test_combat_render_cache_coalesces_same_revision_and_bounds_global_concurrency() -> None:
    async def exercise() -> None:
        cache = CombatRenderCache(max_entries=4, concurrency=1)
        calls = 0
        active = 0
        maximum = 0

        async def render(value: bytes) -> DndCombatRender:
            nonlocal calls, active, maximum
            calls += 1
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
            return _render(value)

        first_key = CombatRenderKey("campaign-1", 7)
        first, second = await asyncio.gather(
            cache.get_or_render(first_key, lambda: render(b"first")),
            cache.get_or_render(first_key, lambda: render(b"unused")),
        )
        assert first is second
        assert calls == 1

        await asyncio.gather(
            cache.get_or_render(CombatRenderKey("campaign-1", 8), lambda: render(b"next")),
            cache.get_or_render(CombatRenderKey("campaign-2", 3), lambda: render(b"other")),
        )
        assert calls == 3
        assert maximum == 1
        await cache.aclose()

    asyncio.run(exercise())


def test_combat_render_cache_ttl_reconciles_a_missing_revision_signal() -> None:
    async def exercise() -> None:
        cache = CombatRenderCache(max_entries=2, concurrency=1, ttl_seconds=0.001)
        calls = 0

        async def render() -> DndCombatRender:
            nonlocal calls
            calls += 1
            return _render(str(calls).encode())

        key = CombatRenderKey("campaign-1", 7)
        await cache.get_or_render(key, render)
        await asyncio.sleep(0.005)
        refreshed = await cache.get_or_render(key, render)
        assert calls == 2
        assert refreshed.render.content == b"2"
        await cache.aclose()

    asyncio.run(exercise())


def test_combat_render_cache_evicts_old_images_to_stay_inside_byte_budget() -> None:
    async def exercise() -> None:
        cache = CombatRenderCache(
            max_entries=8,
            max_bytes=5,
            concurrency=1,
            ttl_seconds=30,
        )
        calls = 0

        async def render(value: bytes) -> DndCombatRender:
            nonlocal calls
            calls += 1
            return _render(value)

        first = CombatRenderKey("campaign-1", 1)
        second = CombatRenderKey("campaign-2", 1)
        await cache.get_or_render(first, lambda: render(b"1234"))
        await cache.get_or_render(second, lambda: render(b"5678"))
        await cache.get_or_render(first, lambda: render(b"1234"))
        assert calls == 3
        await cache.aclose()

    asyncio.run(exercise())
