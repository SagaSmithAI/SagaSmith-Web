from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sagasmith_service.integrations.dnd_mcp import DndCombatRender
from sagasmith_service.observability import (
    COMBAT_RENDER_CACHE_REQUESTS,
    COMBAT_RENDER_SECONDS,
    observe_combat_render,
)


@dataclass(frozen=True, slots=True)
class CombatRenderKey:
    """Identity of one immutable, audience-safe combat render."""

    campaign_id: str
    source_revision: int
    visibility: str = "party_public"
    theme: str = "default"
    size: str = "native"
    renderer_version: str = "dnd-party-public-v1"

    @property
    def artifact_key(self) -> str:
        return ":".join(
            (
                self.campaign_id,
                str(self.source_revision),
                self.visibility,
                self.theme,
                self.size,
                self.renderer_version,
            )
        )


@dataclass(frozen=True, slots=True)
class CachedCombatRender:
    key: CombatRenderKey
    render: DndCombatRender
    cached_at: float


class CombatRenderCache:
    """Bounded revision cache with per-key singleflight and render backpressure."""

    def __init__(
        self,
        *,
        max_entries: int = 8,
        max_bytes: int = 64 * 1024 * 1024,
        concurrency: int = 2,
        ttl_seconds: float = 30,
    ) -> None:
        if max_entries < 1:
            raise ValueError("combat render cache size must be positive")
        if concurrency < 1:
            raise ValueError("combat render concurrency must be positive")
        if max_bytes < 1:
            raise ValueError("combat render cache byte budget must be positive")
        if ttl_seconds <= 0:
            raise ValueError("combat render cache TTL must be positive")
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._total_bytes = 0
        self._ttl_seconds = ttl_seconds
        self._entries: OrderedDict[CombatRenderKey, CachedCombatRender] = OrderedDict()
        self._inflight: dict[CombatRenderKey, asyncio.Task[CachedCombatRender]] = {}
        self._lock = asyncio.Lock()
        self._render_slots = asyncio.Semaphore(concurrency)

    async def get_or_render(
        self,
        key: CombatRenderKey,
        factory: Callable[[], Awaitable[DndCombatRender]],
    ) -> CachedCombatRender:
        async with self._lock:
            cached = self._entries.get(key)
            if (
                cached is not None
                and asyncio.get_running_loop().time() - cached.cached_at
                <= self._ttl_seconds
            ):
                self._entries.move_to_end(key)
                COMBAT_RENDER_CACHE_REQUESTS.labels(result="hit").inc()
                return cached
            if cached is not None:
                self._entries.pop(key, None)
                self._total_bytes -= len(cached.render.content)
            task = self._inflight.get(key)
            if task is None:
                COMBAT_RENDER_CACHE_REQUESTS.labels(result="miss").inc()
                task = asyncio.create_task(self._render(key, factory))
                task.add_done_callback(self._consume_background_exception)
                self._inflight[key] = task
            else:
                COMBAT_RENDER_CACHE_REQUESTS.labels(result="coalesced").inc()
        return await asyncio.shield(task)

    @staticmethod
    def _consume_background_exception(task: asyncio.Task[CachedCombatRender]) -> None:
        if not task.cancelled():
            task.exception()

    async def _render(
        self,
        key: CombatRenderKey,
        factory: Callable[[], Awaitable[DndCombatRender]],
    ) -> CachedCombatRender:
        try:
            async with self._render_slots:
                with observe_combat_render(COMBAT_RENDER_SECONDS):
                    rendered = await factory()
            cached = CachedCombatRender(
                key=key,
                render=rendered,
                cached_at=asyncio.get_running_loop().time(),
            )
            async with self._lock:
                previous = self._entries.pop(key, None)
                if previous is not None:
                    self._total_bytes -= len(previous.render.content)
                self._entries[key] = cached
                self._total_bytes += len(rendered.content)
                self._entries.move_to_end(key)
                while (
                    len(self._entries) > self._max_entries
                    or self._total_bytes > self._max_bytes
                ) and len(self._entries) > 1:
                    _, evicted = self._entries.popitem(last=False)
                    self._total_bytes -= len(evicted.render.content)
            return cached
        finally:
            async with self._lock:
                current = self._inflight.get(key)
                if current is asyncio.current_task():
                    self._inflight.pop(key, None)

    async def aclose(self) -> None:
        async with self._lock:
            tasks = list(self._inflight.values())
            self._inflight.clear()
            self._entries.clear()
            self._total_bytes = 0
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
