from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from threading import Lock
from typing import Any
from weakref import WeakSet

from prometheus_client import Counter, Histogram
from sqlalchemy import event
from sqlalchemy.engine import Engine

REQUESTS = Counter(
    "sagasmith_http_requests_total",
    "HTTP requests",
    ["method", "route", "status"],
)
HTTP_LATENCY_SECONDS = Histogram(
    "sagasmith_http_request_seconds",
    "HTTP request latency",
    ["method", "route"],
)

_UPSTREAM_LABELS = ["system", "operation_class", "status", "transport"]

AGENT_UPSTREAM_SECONDS = Histogram(
    "sagasmith_agent_upstream_seconds",
    "Latency of requests to an Agent or Agent-hosted domain runtime",
    _UPSTREAM_LABELS,
)
MCP_TRANSPORT_SETUP_SECONDS = Histogram(
    "sagasmith_mcp_transport_setup_seconds",
    "Latency to create one isolated MCP transport",
    _UPSTREAM_LABELS,
)
MCP_INITIALIZE_SECONDS = Histogram(
    "sagasmith_mcp_initialize_seconds",
    "Latency to initialize one isolated MCP session",
    _UPSTREAM_LABELS,
)
MCP_EXPOSURE_SECONDS = Histogram(
    "sagasmith_mcp_exposure_seconds",
    "Latency to establish and verify dynamic MCP tool exposure",
    _UPSTREAM_LABELS,
)
MCP_TOOL_SECONDS = Histogram(
    "sagasmith_mcp_tool_seconds",
    "Latency of authoritative MCP tool operations",
    _UPSTREAM_LABELS,
)
ROOM_PROJECTION_BATCH_SECONDS = Histogram(
    "sagasmith_room_projection_batch_seconds",
    "Latency of bounded room projection batches",
    _UPSTREAM_LABELS,
)
ROOM_PROJECTION_JOBS = Histogram(
    "sagasmith_room_projection_jobs",
    "Number of jobs submitted in one bounded room projection batch",
    _UPSTREAM_LABELS,
    buckets=(0, 1, 2, 4, 8, 16, 32, 64, 128, 256, 512),
)

_HOT_PATH_LABELS = ["operation_class", "status"]
_DB_STATEMENT_LABELS = [
    "operation_class",
    "statement_class",
    "execution_context",
    "status",
]
_DB_REQUEST_LABELS = ["operation_class", "execution_context", "status"]
_LATENCY_BUCKETS = (
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
    1,
    2.5,
    5,
)

EVENT_LOOP_LAG_SECONDS = Histogram(
    "sagasmith_event_loop_lag_seconds",
    "Maximum event-loop scheduling lag observed during one hot-path request",
    _HOT_PATH_LABELS,
    buckets=_LATENCY_BUCKETS,
)
DB_STATEMENT_SECONDS = Histogram(
    "sagasmith_db_statement_seconds",
    "Database cursor execution latency for selected request hot paths",
    _DB_STATEMENT_LABELS,
    buckets=_LATENCY_BUCKETS,
)
DB_REQUEST_SECONDS = Histogram(
    "sagasmith_db_request_seconds",
    "Sum of database cursor latency within one selected hot-path request",
    _DB_REQUEST_LABELS,
    buckets=_LATENCY_BUCKETS,
)
DB_STATEMENTS_PER_REQUEST = Histogram(
    "sagasmith_db_statements_per_request",
    "Database statements executed within one selected hot-path request",
    _DB_REQUEST_LABELS,
    buckets=(0, 1, 2, 4, 8, 16, 32, 64, 128, 256),
)

HOT_PATH_OPERATIONS = frozenset(
    {"room_action", "agent_message", "projection_refresh", "activity_callback"}
)
_EXECUTION_CONTEXTS = ("event_loop", "worker")
_STATEMENT_CLASSES = frozenset({"select", "insert", "update", "delete", "transaction"})


@dataclass
class HotPathRequestObservation:
    """Mutable request-local DB totals shared with FastAPI worker threads."""

    operation_class: str
    _seconds: dict[str, float] = field(
        default_factory=lambda: {name: 0.0 for name in _EXECUTION_CONTEXTS}
    )
    _counts: dict[str, int] = field(
        default_factory=lambda: {name: 0 for name in _EXECUTION_CONTEXTS}
    )
    _lock: Lock = field(default_factory=Lock)

    def __post_init__(self) -> None:
        if self.operation_class not in HOT_PATH_OPERATIONS:
            raise ValueError(f"unsupported hot-path operation: {self.operation_class}")

    def record(self, *, execution_context: str, elapsed: float) -> None:
        if execution_context not in _EXECUTION_CONTEXTS:
            raise ValueError(f"unsupported DB execution context: {execution_context}")
        with self._lock:
            self._seconds[execution_context] += elapsed
            self._counts[execution_context] += 1

    def snapshot(self) -> dict[str, tuple[float, int]]:
        with self._lock:
            return {
                name: (self._seconds[name], self._counts[name])
                for name in _EXECUTION_CONTEXTS
            }


_HOT_PATH_OBSERVATION: ContextVar[HotPathRequestObservation | None] = ContextVar(
    "sagasmith_hot_path_observation",
    default=None,
)
_INSTRUMENTED_ENGINES: WeakSet[Engine] = WeakSet()
_INSTRUMENTED_ENGINES_LOCK = Lock()
_EXECUTION_RECORD_ATTRIBUTE = "_sagasmith_db_observation"


def hot_path_operation(method: str, path: str) -> str | None:
    """Return one bounded operation label without retaining resource identifiers."""

    parts = path.strip("/").split("/")
    if len(parts) < 5 or parts[:2] != ["api", "campaigns"]:
        return None
    normalized_method = method.upper()
    if (
        normalized_method == "POST"
        and len(parts) == 7
        and parts[3:5] == ["agent", "conversations"]
        and parts[6] == "messages"
    ):
        return "agent_message"
    if parts[3] != "room":
        return None
    if normalized_method == "POST" and len(parts) == 5 and parts[4] == "messages":
        return "room_action"
    if (
        normalized_method == "POST"
        and len(parts) == 6
        and parts[4:6] == ["panel", "actions"]
    ):
        return "room_action"
    if normalized_method == "GET" and len(parts) == 5 and parts[4] == "panel":
        return "projection_refresh"
    if (
        normalized_method == "POST"
        and len(parts) == 6
        and parts[4] == "internal-activity"
    ):
        return "activity_callback"
    return None


def bind_hot_path_observation(
    observation: HotPathRequestObservation,
) -> Token[HotPathRequestObservation | None]:
    return _HOT_PATH_OBSERVATION.set(observation)


def reset_hot_path_observation(token: Token[HotPathRequestObservation | None]) -> None:
    _HOT_PATH_OBSERVATION.reset(token)


def _statement_class(statement: str) -> str:
    head = statement.lstrip().partition(" ")[0].casefold()
    if head in _STATEMENT_CLASSES:
        return head
    return "other"


def _execution_context() -> str:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return "worker"
    return "event_loop"


def _before_cursor_execute(
    _connection: Any,
    _cursor: Any,
    statement: str,
    _parameters: Any,
    execution_context: Any,
    _executemany: bool,
) -> None:
    observation = _HOT_PATH_OBSERVATION.get()
    record = None
    if observation is not None:
        record = (
            time.perf_counter(),
            observation,
            _statement_class(statement),
            _execution_context(),
        )
    setattr(execution_context, _EXECUTION_RECORD_ATTRIBUTE, record)


def _finish_cursor_execute(execution_context: Any, *, status: str) -> None:
    record = getattr(execution_context, _EXECUTION_RECORD_ATTRIBUTE, None)
    if record is None:
        return
    setattr(execution_context, _EXECUTION_RECORD_ATTRIBUTE, None)
    started, observation, statement_class, execution_site = record
    elapsed = time.perf_counter() - started
    observation.record(execution_context=execution_site, elapsed=elapsed)
    DB_STATEMENT_SECONDS.labels(
        operation_class=observation.operation_class,
        statement_class=statement_class,
        execution_context=execution_site,
        status=status,
    ).observe(elapsed)


def _after_cursor_execute(
    _connection: Any,
    _cursor: Any,
    _statement: str,
    _parameters: Any,
    execution_context: Any,
    _executemany: bool,
) -> None:
    _finish_cursor_execute(execution_context, status="success")


def _handle_cursor_error(exception_context: Any) -> None:
    execution_context = getattr(exception_context, "execution_context", None)
    if execution_context is not None:
        _finish_cursor_execute(execution_context, status="error")


def install_database_observability(engine: Engine) -> None:
    """Attach request-scoped cursor metrics exactly once to one sync engine."""

    with _INSTRUMENTED_ENGINES_LOCK:
        if engine in _INSTRUMENTED_ENGINES:
            return
        event.listen(engine, "before_cursor_execute", _before_cursor_execute)
        event.listen(engine, "after_cursor_execute", _after_cursor_execute)
        event.listen(engine, "handle_error", _handle_cursor_error)
        _INSTRUMENTED_ENGINES.add(engine)


async def sample_max_event_loop_lag(
    stop: asyncio.Event,
    *,
    interval_seconds: float = 0.01,
    started: asyncio.Event | None = None,
) -> float:
    """Measure scheduling drift until ``stop`` without delaying request completion."""

    if interval_seconds <= 0:
        raise ValueError("event-loop sampling interval must be positive")
    loop = asyncio.get_running_loop()
    expected = loop.time() + interval_seconds
    maximum = 0.0
    if started is not None:
        started.set()
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            now = loop.time()
            maximum = max(maximum, max(0.0, now - expected))
            expected = now + interval_seconds
            continue
        now = loop.time()
        maximum = max(maximum, max(0.0, now - expected))
        return maximum


def observe_hot_path_request(
    observation: HotPathRequestObservation,
    *,
    status: str,
    event_loop_lag_seconds: float,
) -> None:
    EVENT_LOOP_LAG_SECONDS.labels(
        operation_class=observation.operation_class,
        status=status,
    ).observe(event_loop_lag_seconds)
    for execution_context, (elapsed, count) in observation.snapshot().items():
        if count == 0:
            continue
        labels = {
            "operation_class": observation.operation_class,
            "execution_context": execution_context,
            "status": status,
        }
        DB_REQUEST_SECONDS.labels(**labels).observe(elapsed)
        DB_STATEMENTS_PER_REQUEST.labels(**labels).observe(count)


@contextmanager
def observe_latency(
    metric: Histogram,
    *,
    system: str,
    operation_class: str,
    transport: str,
) -> Iterator[None]:
    """Observe one bounded-cardinality phase and preserve the original exception."""

    started = time.perf_counter()
    status = "success"
    try:
        yield
    except BaseException:
        status = "error"
        raise
    finally:
        metric.labels(
            system=system,
            operation_class=operation_class,
            status=status,
            transport=transport,
        ).observe(time.perf_counter() - started)
