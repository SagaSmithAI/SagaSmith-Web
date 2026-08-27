import asyncio

import pytest

from sagasmith_service.api.rooms import _bounded_map_ordered


def test_bounded_map_preserves_input_order_and_limits_concurrency() -> None:
    async def scenario() -> None:
        active = 0
        maximum = 0
        completion_order: list[int] = []
        first_wave_ready = asyncio.Event()

        async def worker(item: int) -> int:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            if active == 3:
                first_wave_ready.set()
            await first_wave_ready.wait()
            await asyncio.sleep((8 - item) * 0.001)
            completion_order.append(item)
            active -= 1
            return item * 10

        results = await _bounded_map_ordered(list(range(9)), worker, limit=3)

        assert results == [item * 10 for item in range(9)]
        assert maximum == 3
        assert completion_order != list(range(9))

    asyncio.run(scenario())


def test_bounded_map_cancels_in_flight_work_and_preserves_first_error() -> None:
    class ProjectionError(RuntimeError):
        pass

    async def scenario() -> None:
        blocked_started = asyncio.Event()
        blocked_cancelled = asyncio.Event()

        async def worker(item: int) -> int:
            if item == 1:
                await blocked_started.wait()
                raise ProjectionError("projection failed")
            if item == 0:
                blocked_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    blocked_cancelled.set()
                    raise
            return item

        with pytest.raises(ProjectionError, match="projection failed"):
            await _bounded_map_ordered([0, 1, 2], worker, limit=2)
        assert blocked_cancelled.is_set()

    asyncio.run(scenario())
