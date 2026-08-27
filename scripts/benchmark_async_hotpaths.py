from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from prometheus_client import REGISTRY
from sqlalchemy import event, select

from sagasmith_service.api.rooms import _activity_token
from sagasmith_service.config import Settings
from sagasmith_service.database import make_engine
from sagasmith_service.integrations.agent import AgentResult
from sagasmith_service.main import create_app
from sagasmith_service.models import AgentRun
from sagasmith_service.observability import _HOT_PATH_OBSERVATION

_STATEMENT_CLASSES = ("select", "insert", "update", "delete", "transaction", "other")
_EXECUTION_CONTEXTS = ("async_driver", "event_loop", "worker")
_STATUSES = ("success", "error")
_PASSWORD = "benchmark-only-correct-horse-battery-staple"
_DB_HISTOGRAM_BOUNDS = (
    0.0001,
    0.00025,
    0.0005,
    0.001,
    0.0025,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    math.inf,
)


def _histogram_bound_label(bound: float) -> str:
    if math.isinf(bound):
        return "+Inf"
    return str(bound)


class RequestTransactionTracker:
    """Track request-owned sync and async connections without changing execution sites."""

    def __init__(self) -> None:
        self._active: dict[int, set[int]] = {}
        self._lock = threading.Lock()

    def install(self, engine: Any) -> None:
        @event.listens_for(engine, "begin")
        def begin(connection: Any) -> None:
            observation = _HOT_PATH_OBSERVATION.get()
            if observation is None:
                return
            with self._lock:
                self._active.setdefault(id(observation), set()).add(id(connection))

        def finish(connection: Any) -> None:
            observation = _HOT_PATH_OBSERVATION.get()
            if observation is None:
                return
            with self._lock:
                connections = self._active.get(id(observation))
                if connections is None:
                    return
                connections.discard(id(connection))
                if not connections:
                    self._active.pop(id(observation), None)

        event.listen(engine, "commit", finish)
        event.listen(engine, "rollback", finish)

    def current_request_has_transaction(self) -> bool:
        observation = _HOT_PATH_OBSERVATION.get()
        if observation is None:
            return False
        with self._lock:
            return bool(self._active.get(id(observation)))


class BenchmarkRuntime:
    """Deterministic upstream used to isolate SagaSmith Web and database latency."""

    def __init__(
        self,
        upstream_delay_seconds: float,
        transaction_tracker: RequestTransactionTracker,
    ) -> None:
        self.upstream_delay_seconds = upstream_delay_seconds
        self.transaction_tracker = transaction_tracker
        self._scenario: str | None = None
        self._transaction_probes: dict[str, list[tuple[str, bool]]] = {}

    def begin_scenario(self, scenario: str) -> None:
        self._scenario = scenario
        self._transaction_probes[scenario] = []

    def transaction_summary(self, scenario: str) -> dict[str, dict[str, int]]:
        summary: dict[str, dict[str, int]] = {}
        for operation, in_transaction in self._transaction_probes.get(scenario, []):
            item = summary.setdefault(
                operation,
                {"in_transaction": 0, "released": 0, "samples": 0},
            )
            item["samples"] += 1
            item["in_transaction" if in_transaction else "released"] += 1
        return summary

    async def _upstream_wait(self, operation: str) -> None:
        if self._scenario is not None:
            self._transaction_probes[self._scenario].append(
                (operation, self.transaction_tracker.current_request_has_transaction())
            )
        await asyncio.sleep(self.upstream_delay_seconds)

    async def probe(self) -> None:
        return None

    async def create_campaign(self, **_arguments: Any) -> dict[str, Any]:
        return {"id": f"benchmark-{uuid.uuid4()}", "revision": 1}

    async def get_campaign(self, **arguments: Any) -> dict[str, Any]:
        await self._upstream_wait("mcp.campaign")
        return {
            "result": {
                "id": arguments["campaign_id"],
                "revision": 1,
                "effective_game_phase": "play",
            }
        }

    async def get_panel_state(self, **arguments: Any) -> dict[str, Any]:
        await self._upstream_wait("mcp.panel_state")
        return {
            "campaign": {"id": arguments["campaign_id"], "revision": 1},
            "phase": "play",
            "revision": 1,
            "party": {"members": []},
            "characters": [],
            "modules": [],
            "current_module": None,
            "combat": None,
        }

    async def complete(self, **arguments: Any) -> AgentResult:
        await self._upstream_wait("agent.complete")
        context = arguments.get("context") or {}
        structured_output = None
        if context.get("response_contract"):
            structured_output = {
                "schema": "sagasmith.room-turn/v1",
                "run_id": context["run_id"],
                "messages": [
                    {
                        "output_id": "main",
                        "audience": {"kind": "public", "actor_refs": []},
                        "blocks": [
                            {
                                "type": "narration",
                                "block_id": "benchmark",
                                "text": "Benchmark response.",
                            }
                        ],
                    }
                ],
                "suggestions": [],
            }
        return AgentResult(
            content="Benchmark response.",
            request_id=f"benchmark-{uuid.uuid4()}",
            model="benchmark-runtime",
            prompt_tokens=1,
            completion_tokens=1,
            structured_output=structured_output,
        )

    async def set_game_phase(self, **_arguments: Any) -> dict[str, Any]:
        await self._upstream_wait("mcp.phase.set")
        return {"result": {"campaign_revision": 2}}


@dataclass
class Lane:
    index: int
    client: httpx.AsyncClient
    campaign_id: str
    conversation_id: str
    activity_run_id: str
    activity_token: str


def _sample_value(name: str, labels: dict[str, str]) -> float:
    value = REGISTRY.get_sample_value(name, labels)
    return float(value or 0.0)


def _db_metric_snapshot(operation_class: str) -> dict[tuple[str, str, str], dict[str, Any]]:
    snapshot: dict[tuple[str, str, str], dict[str, Any]] = {}
    for statement_class in _STATEMENT_CLASSES:
        for execution_context in _EXECUTION_CONTEXTS:
            for status in _STATUSES:
                labels = {
                    "operation_class": operation_class,
                    "statement_class": statement_class,
                    "execution_context": execution_context,
                    "status": status,
                }
                key = (statement_class, execution_context, status)
                snapshot[key] = {
                    "count": _sample_value("sagasmith_db_statement_seconds_count", labels),
                    "seconds": _sample_value("sagasmith_db_statement_seconds_sum", labels),
                    "buckets": tuple(
                        _sample_value(
                            "sagasmith_db_statement_seconds_bucket",
                            {**labels, "le": _histogram_bound_label(bound)},
                        )
                        for bound in _DB_HISTOGRAM_BOUNDS
                    ),
                }
    return snapshot


def _db_metric_delta(
    before: dict[tuple[str, str, str], dict[str, Any]],
    after: dict[tuple[str, str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for key in sorted(after):
        count = int(round(after[key]["count"] - before[key]["count"]))
        seconds = after[key]["seconds"] - before[key]["seconds"]
        if count <= 0:
            continue
        statement_class, execution_context, status = key
        buckets = tuple(
            int(round(after_count - before_count))
            for before_count, after_count in zip(
                before[key]["buckets"], after[key]["buckets"], strict=True
            )
        )
        threshold = 0.95 * count
        p95_bound = next(
            (
                bound
                for bound, cumulative_count in zip(_DB_HISTOGRAM_BOUNDS, buckets, strict=True)
                if cumulative_count >= threshold
            ),
            math.inf,
        )
        at_or_below_five_ms = buckets[_DB_HISTOGRAM_BOUNDS.index(0.005)]
        result.append(
            {
                "statement_class": statement_class,
                "execution_context": execution_context,
                "status": status,
                "count": count,
                "seconds": round(seconds, 9),
                "p95_upper_bound_ms": (
                    round(p95_bound * 1000, 3) if math.isfinite(p95_bound) else None
                ),
                "p95_above_max_bucket": math.isinf(p95_bound),
                "p95_exceeds_5ms": at_or_below_five_ms < threshold,
            }
        )
    return result


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


async def _lag_samples(stop: asyncio.Event, interval_seconds: float) -> list[float]:
    samples: list[float] = []
    loop = asyncio.get_running_loop()
    expected = loop.time() + interval_seconds
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            now = loop.time()
            samples.append(max(0.0, now - expected))
            expected = now + interval_seconds
            continue
        now = loop.time()
        samples.append(max(0.0, now - expected))
        return samples


def _checked(response: httpx.Response, *, operation: str) -> None:
    if response.status_code >= 400:
        raise RuntimeError(
            f"{operation} returned HTTP {response.status_code}: {response.text[:500]}"
        )


async def _prepare_lane(
    *,
    app: Any,
    transport: httpx.ASGITransport,
    index: int,
) -> Lane:
    client = httpx.AsyncClient(
        transport=transport,
        base_url="http://benchmark.local",
        headers={"Origin": "http://benchmark.local"},
    )
    prefix = f"lane-{index}-{uuid.uuid4().hex}"
    response = await client.post(
        "/api/auth/register",
        json={
            "email": f"{prefix}@example.com",
            "password": _PASSWORD,
            "display_name": f"Benchmark lane {index}",
        },
    )
    _checked(response, operation="register")
    response = await client.post(
        "/api/campaigns",
        headers={"Idempotency-Key": f"{prefix}-campaign"},
        json={"name": f"Benchmark campaign {index}"},
    )
    _checked(response, operation="campaign setup")
    campaign_id = str(response.json()["id"])
    response = await client.post(
        f"/api/campaigns/{campaign_id}/agent/conversations",
        json={"title": "Benchmark conversation"},
    )
    _checked(response, operation="conversation setup")
    conversation_id = str(response.json()["id"])

    response = await client.post(
        f"/api/campaigns/{campaign_id}/room/messages",
        headers={"Idempotency-Key": f"{prefix}-activity-seed"},
        json={"content": "Seed activity callback run.", "mode": "action"},
    )
    _checked(response, operation="activity setup")
    with app.state.session_factory() as session:
        run = session.scalar(
            select(AgentRun)
            .where(AgentRun.campaign_id == campaign_id)
            .order_by(AgentRun.created_at.desc())
        )
        if run is None:
            raise RuntimeError("activity setup did not create an Agent run")
        run.status = "running"
        activity_run_id = run.id
        session.commit()
    activity_token = _activity_token(
        app.state.settings.session_secret,
        campaign_id,
        activity_run_id,
    )
    return Lane(
        index=index,
        client=client,
        campaign_id=campaign_id,
        conversation_id=conversation_id,
        activity_run_id=activity_run_id,
        activity_token=activity_token,
    )


async def _run_scenario(
    *,
    name: str,
    operation_class: str,
    lanes: list[Lane],
    iterations: int,
    lag_interval_seconds: float,
    request: Callable[[Lane, int], Awaitable[httpx.Response]],
    runtime: BenchmarkRuntime,
) -> dict[str, Any]:
    runtime.begin_scenario(name)
    before = _db_metric_snapshot(operation_class)
    latencies: list[float] = []
    statuses: Counter[int] = Counter()
    stop = asyncio.Event()
    sampler = asyncio.create_task(_lag_samples(stop, lag_interval_seconds))

    async def run_lane(lane: Lane) -> None:
        for iteration in range(iterations):
            started = time.perf_counter()
            response = await request(lane, iteration)
            latencies.append(time.perf_counter() - started)
            statuses[response.status_code] += 1

    started = time.perf_counter()
    try:
        await asyncio.gather(*(run_lane(lane) for lane in lanes))
    finally:
        stop.set()
    lag = await sampler
    wall_seconds = time.perf_counter() - started
    after = _db_metric_snapshot(operation_class)
    db = _db_metric_delta(before, after)
    event_loop_db_seconds = sum(
        item["seconds"] for item in db if item["execution_context"] == "event_loop"
    )
    total_db_seconds = sum(item["seconds"] for item in db)
    total_request_wall_seconds = sum(latencies)
    return {
        "name": name,
        "operation_class": operation_class,
        "requests": len(latencies),
        "concurrency": len(lanes),
        "iterations_per_lane": iterations,
        "status_counts": {str(key): value for key, value in sorted(statuses.items())},
        "success_rate": round(
            sum(count for status, count in statuses.items() if status < 400) / len(latencies),
            6,
        ),
        "failure_rate": round(
            sum(count for status, count in statuses.items() if status >= 400) / len(latencies),
            6,
        ),
        "wall_seconds": round(wall_seconds, 6),
        "requests_per_second": round(len(latencies) / wall_seconds, 3),
        "request_latency_ms": {
            "p50": round(_percentile(latencies, 0.5) * 1000, 3),
            "p95": round(_percentile(latencies, 0.95) * 1000, 3),
            "p99": round(_percentile(latencies, 0.99) * 1000, 3),
            "max": round(max(latencies, default=0.0) * 1000, 3),
        },
        "event_loop_lag_ms": {
            "p95": round(_percentile(lag, 0.95) * 1000, 3),
            "max": round(max(lag, default=0.0) * 1000, 3),
            "samples": len(lag),
        },
        "db": db,
        "event_loop_db_seconds": round(event_loop_db_seconds, 9),
        "event_loop_db_fraction": (
            round(event_loop_db_seconds / total_db_seconds, 6) if total_db_seconds > 0 else 0.0
        ),
        "event_loop_db_fraction_denominator": "total_db_cursor_seconds",
        "event_loop_db_fraction_of_request_wall_time": (
            round(event_loop_db_seconds / total_request_wall_seconds, 6)
            if total_request_wall_seconds > 0
            else 0.0
        ),
        "upstream_transaction_probes": runtime.transaction_summary(name),
    }


async def run_benchmark(
    *,
    database_url: str,
    concurrency: int,
    iterations: int,
    upstream_delay_seconds: float,
    lag_interval_seconds: float,
    simulated_db_delay_seconds: float,
    private_root: Path,
    scenario_names: tuple[str, ...] = (
        "agent_message",
        "room_action",
        "projection_refresh",
        "panel_action",
        "activity_callback",
    ),
) -> dict[str, Any]:
    engine = make_engine(database_url)
    if simulated_db_delay_seconds > 0:

        @event.listens_for(engine, "before_cursor_execute")
        def simulated_db_delay(*_arguments: Any) -> None:
            time.sleep(simulated_db_delay_seconds)

    transaction_tracker = RequestTransactionTracker()
    transaction_tracker.install(engine)
    runtime = BenchmarkRuntime(upstream_delay_seconds, transaction_tracker)
    settings = Settings(
        env="test",
        database_url=database_url,
        session_secret="benchmark-session-secret-at-least-thirty-two-characters",
        public_origin="http://benchmark.local",
        private_storage_dir=str(private_root / "private"),
        exchange_dir=str(private_root / "exchange"),
    )
    app = create_app(
        settings,
        engine,
        runtime,
        runtime,
        coc_runtime=runtime,
        narrative_runtime=runtime,
    )
    transaction_tracker.install(app.state.async_engine.sync_engine)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    lanes: list[Lane] = []
    try:
        async with app.router.lifespan_context(app):
            for index in range(concurrency):
                lanes.append(await _prepare_lane(app=app, transport=transport, index=index))

            async def agent_message(lane: Lane, iteration: int) -> httpx.Response:
                return await lane.client.post(
                    f"/api/campaigns/{lane.campaign_id}/agent/conversations/"
                    f"{lane.conversation_id}/messages",
                    headers={"Idempotency-Key": f"agent-load-{lane.index}-{iteration}"},
                    json={"content": f"Agent message {iteration}"},
                )

            async def room_action(lane: Lane, iteration: int) -> httpx.Response:
                return await lane.client.post(
                    f"/api/campaigns/{lane.campaign_id}/room/messages",
                    headers={"Idempotency-Key": f"room-load-{lane.index}-{iteration}"},
                    json={"content": f"Room action {iteration}", "mode": "action"},
                )

            async def projection_refresh(lane: Lane, _iteration: int) -> httpx.Response:
                return await lane.client.get(f"/api/campaigns/{lane.campaign_id}/room/panel")

            async def panel_action(lane: Lane, iteration: int) -> httpx.Response:
                return await lane.client.post(
                    f"/api/campaigns/{lane.campaign_id}/room/panel/actions",
                    headers={"Idempotency-Key": f"panel-load-{lane.index}-{iteration}"},
                    json={"action": "phase.set", "payload": {"phase": "lobby"}},
                )

            async def activity_callback(lane: Lane, iteration: int) -> httpx.Response:
                return await lane.client.post(
                    f"/api/campaigns/{lane.campaign_id}/room/internal-activity/"
                    f"{lane.activity_run_id}",
                    headers={"Authorization": f"Bearer {lane.activity_token}"},
                    json={
                        "schema": "sagasmith.room-activity/v1",
                        "run_id": lane.activity_run_id,
                        "activity_id": f"benchmark-{lane.index}-{iteration}",
                        "audience": {"kind": "public"},
                        "code": "reviewing_rules",
                        "state": "started",
                    },
                )

            scenarios = []
            for name, operation_class, request in (
                ("agent_message", "agent_message", agent_message),
                ("room_action", "room_action", room_action),
                ("projection_refresh", "projection_refresh", projection_refresh),
                ("panel_action", "room_action", panel_action),
                ("activity_callback", "activity_callback", activity_callback),
            ):
                if name not in scenario_names:
                    continue
                scenarios.append(
                    await _run_scenario(
                        name=name,
                        operation_class=operation_class,
                        lanes=lanes,
                        iterations=iterations,
                        lag_interval_seconds=lag_interval_seconds,
                        request=request,
                        runtime=runtime,
                    )
                )
    finally:
        await asyncio.gather(*(lane.client.aclose() for lane in lanes), return_exceptions=True)
        engine.dispose()

    return {
        "schema": "sagasmith.async-hotpath-benchmark/v1",
        "database": "sqlite" if database_url.startswith("sqlite") else "postgresql",
        "concurrency": concurrency,
        "iterations_per_lane": iterations,
        "upstream_delay_ms": upstream_delay_seconds * 1000,
        "lag_interval_ms": lag_interval_seconds * 1000,
        "simulated_db_delay_ms": simulated_db_delay_seconds * 1000,
        "simulated_delay_is_calibration_only": simulated_db_delay_seconds > 0,
        "selected_scenarios": list(scenario_names),
        "scenarios": scenarios,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Benchmark synchronous SQLAlchemy use in selected SagaSmith Web async paths."
    )
    parser.add_argument(
        "--database-url",
        help="Optional dedicated database URL. The default is a temporary local SQLite file.",
    )
    parser.add_argument(
        "--allow-database-writes",
        action="store_true",
        help="Required with any explicit URL because the harness creates persistent test rows.",
    )
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--upstream-delay-ms", type=float, default=5.0)
    parser.add_argument("--lag-interval-ms", type=float, default=2.0)
    parser.add_argument(
        "--simulated-db-delay-ms",
        type=float,
        default=0.0,
        help="Calibration only; never use an injected delay as migration evidence.",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        choices=(
            "agent_message",
            "room_action",
            "projection_refresh",
            "panel_action",
            "activity_callback",
        ),
        help="Repeat to run a subset. The default runs all five benchmark scenarios.",
    )
    parser.add_argument("--output", type=Path)
    return parser


def _run_with_compatible_event_loop(coroutine: Any, *, database_url: str) -> Any:
    if sys.platform == "win32" and database_url.startswith("postgresql+psycopg://"):
        # Psycopg's async connection cannot run on Windows' default Proactor loop.
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            return runner.run(coroutine)
    return asyncio.run(coroutine)


def main() -> None:
    arguments = _parser().parse_args()
    if arguments.concurrency <= 0 or arguments.iterations <= 0:
        raise SystemExit("--concurrency and --iterations must be positive")
    if arguments.upstream_delay_ms < 0 or arguments.simulated_db_delay_ms < 0:
        raise SystemExit("delay values cannot be negative")
    if arguments.lag_interval_ms <= 0:
        raise SystemExit("--lag-interval-ms must be positive")
    if arguments.database_url and not arguments.allow_database_writes:
        raise SystemExit(
            "explicit database runs write benchmark rows; use a disposable database and pass "
            "--allow-database-writes"
        )
    if arguments.database_url and not arguments.database_url.startswith(
        ("sqlite", "postgresql+psycopg://")
    ):
        raise SystemExit("only SQLite and postgresql+psycopg URLs are supported")

    with tempfile.TemporaryDirectory(prefix="sagasmith-hotpaths-") as temporary:
        temporary_root = Path(temporary)
        database_url = arguments.database_url or (
            f"sqlite:///{(temporary_root / 'benchmark.db').as_posix()}"
        )
        result = _run_with_compatible_event_loop(
            run_benchmark(
                database_url=database_url,
                concurrency=arguments.concurrency,
                iterations=arguments.iterations,
                upstream_delay_seconds=arguments.upstream_delay_ms / 1000,
                lag_interval_seconds=arguments.lag_interval_ms / 1000,
                simulated_db_delay_seconds=arguments.simulated_db_delay_ms / 1000,
                private_root=temporary_root,
                scenario_names=tuple(
                    arguments.scenario
                    or (
                        "agent_message",
                        "room_action",
                        "projection_refresh",
                        "panel_action",
                        "activity_callback",
                    )
                ),
            ),
            database_url=database_url,
        )
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if arguments.output:
        arguments.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
