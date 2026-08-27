from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

from prometheus_client import Counter, Histogram

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
