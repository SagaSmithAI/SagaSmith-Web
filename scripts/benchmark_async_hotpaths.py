from __future__ import annotations

import argparse
import asyncio
import json
import math
import tempfile
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

_STATEMENT_CLASSES = ("select", "insert", "update", "delete", "transaction", "other")
_EXECUTION_CONTEXTS = ("async_driver", "event_loop", "worker")
_STATUSES = ("success", "error")
_PASSWORD = "benchmark-only-correct-horse-battery-staple"


class BenchmarkRuntime:
    """Deterministic upstream used to isolate SagaSmith Web and database latency."""

    def __init__(self, upstream_delay_seconds: float) -> None:
        self.upstream_delay_seconds = upstream_delay_seconds

    async def probe(self) -> None:
        return None

    async def create_campaign(self, **_arguments: Any) -> dict[str, Any]:
        return {"id": f"benchmark-{uuid.uuid4()}", "revision": 1}

    async def get_campaign(self, **arguments: Any) -> dict[str, Any]:
        await asyncio.sleep(self.upstream_delay_seconds)
        return {
            "result": {
                "id": arguments["campaign_id"],
                "revision": 1,
                "effective_game_phase": "play",
            }
        }

    async def get_panel_state(self, **arguments: Any) -> dict[str, Any]:
        await asyncio.sleep(self.upstream_delay_seconds)
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
        await asyncio.sleep(self.upstream_delay_seconds)
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


def _db_metric_snapshot(operation_class: str) -> dict[tuple[str, str, str], tuple[float, float]]:
    snapshot: dict[tuple[str, str, str], tuple[float, float]] = {}
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
                snapshot[key] = (
                    _sample_value("sagasmith_db_statement_seconds_count", labels),
                    _sample_value("sagasmith_db_statement_seconds_sum", labels),
                )
    return snapshot


def _db_metric_delta(
    before: dict[tuple[str, str, str], tuple[float, float]],
    after: dict[tuple[str, str, str], tuple[float, float]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for key in sorted(after):
        count = int(round(after[key][0] - before[key][0]))
        seconds = after[key][1] - before[key][1]
        if count <= 0:
            continue
        statement_class, execution_context, status = key
        result.append(
            {
                "statement_class": statement_class,
                "execution_context": execution_context,
                "status": status,
                "count": count,
                "seconds": round(seconds, 9),
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
) -> dict[str, Any]:
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
            round(event_loop_db_seconds / total_db_seconds, 6)
            if total_db_seconds > 0
            else 0.0
        ),
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
        "activity_callback",
    ),
) -> dict[str, Any]:
    engine = make_engine(database_url)
    if simulated_db_delay_seconds > 0:

        @event.listens_for(engine, "before_cursor_execute")
        def simulated_db_delay(*_arguments: Any) -> None:
            time.sleep(simulated_db_delay_seconds)

    runtime = BenchmarkRuntime(upstream_delay_seconds)
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
                return await lane.client.get(
                    f"/api/campaigns/{lane.campaign_id}/room/panel"
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
            "activity_callback",
        ),
        help="Repeat to run a subset. The default runs all four hot paths.",
    )
    parser.add_argument("--output", type=Path)
    return parser


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
        result = asyncio.run(
            run_benchmark(
                database_url=database_url,
                concurrency=arguments.concurrency,
                iterations=arguments.iterations,
                upstream_delay_seconds=arguments.upstream_delay_ms / 1000,
                lag_interval_seconds=arguments.lag_interval_ms / 1000,
                simulated_db_delay_seconds=arguments.simulated_db_delay_ms / 1000,
                private_root=temporary_root,
                scenario_names=tuple(arguments.scenario or (
                    "agent_message",
                    "room_action",
                    "projection_refresh",
                    "activity_callback",
                )),
            )
        )
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if arguments.output:
        arguments.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
