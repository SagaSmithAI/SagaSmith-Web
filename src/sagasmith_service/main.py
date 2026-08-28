from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine

from sagasmith_service import __version__
from sagasmith_service.api.admin import router as admin_router
from sagasmith_service.api.agent import router as agent_router
from sagasmith_service.api.audit import router as audit_router
from sagasmith_service.api.auth import router as auth_router
from sagasmith_service.api.campaigns import router as campaign_router
from sagasmith_service.api.community import router as community_router
from sagasmith_service.api.identities import router as identities_router
from sagasmith_service.api.invites import router as invites_router
from sagasmith_service.api.modules import NOTIFICATION_ROUTER
from sagasmith_service.api.modules import router as modules_router
from sagasmith_service.api.operations import router as operations_router
from sagasmith_service.api.packs import router as packs_router
from sagasmith_service.api.rooms import execute_room_turn_job, record_room_job_failure
from sagasmith_service.api.rooms import router as rooms_router
from sagasmith_service.api.usage import router as usage_router
from sagasmith_service.audit import bind_request_id, reset_request_id
from sagasmith_service.combat_render_cache import CombatRenderCache
from sagasmith_service.config import Settings, get_settings
from sagasmith_service.database import (
    Base,
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from sagasmith_service.integrations.agent import AgentRuntime, HttpAgentRuntime
from sagasmith_service.integrations.coc_mcp import StreamableHttpCocRuntime
from sagasmith_service.integrations.dnd_mcp import DndRuntime, StreamableHttpDndRuntime
from sagasmith_service.integrations.narrative_mcp import HttpNarrativeRuntime
from sagasmith_service.observability import (
    HTTP_LATENCY_SECONDS,
    REQUESTS,
    HotPathRequestObservation,
    bind_hot_path_observation,
    hot_path_operation,
    install_database_observability,
    observe_hot_path_request,
    reset_hot_path_observation,
    sample_max_event_loop_lag,
)
from sagasmith_service.rate_limit import (
    MemoryRateLimiter,
    RateLimiter,
    RateLimiterUnavailableError,
    RedisRateLimiter,
    opaque_rate_key,
)
from sagasmith_service.realtime import (
    OutboxDispatcher,
    RealtimeHub,
    install_transactional_outbox,
)
from sagasmith_service.room_jobs import RoomTurnJobProcessor
from sagasmith_service.security import SESSION_COOKIE
from sagasmith_service.storage import LocalPrivateStorage, S3PrivateStorage

logger = logging.getLogger("sagasmith_service.http")
LATENCY = HTTP_LATENCY_SECONDS
SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{8,100}$")


def create_app(
    settings: Settings | None = None,
    engine: Engine | None = None,
    dnd_runtime: DndRuntime | None = None,
    agent_runtime: AgentRuntime | None = None,
    rate_limiter: RateLimiter | None = None,
    coc_runtime: object | None = None,
    narrative_runtime: object | None = None,
    async_engine: AsyncEngine | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    engine = engine or make_engine(settings.database_url)
    install_database_observability(engine)
    async_engine = async_engine or make_async_engine(
        engine.url.render_as_string(hide_password=False)
    )
    install_database_observability(async_engine.sync_engine)
    if settings.env in {"development", "test"}:
        Base.metadata.create_all(engine)
    install_transactional_outbox()
    session_factory = make_session_factory(engine)
    realtime_hub = RealtimeHub(
        settings.redis_url if settings.rate_limit_backend == "redis" else None
    )
    outbox_dispatcher = OutboxDispatcher(session_factory, realtime_hub)
    managed_http_clients: dict[str, httpx.AsyncClient] = {}

    def managed_http_client(name: str, **kwargs: Any) -> httpx.AsyncClient:
        client = httpx.AsyncClient(**kwargs)
        managed_http_clients[name] = client
        return client

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await realtime_hub.start()
        outbox_dispatcher.start()
        _app.state.room_turn_jobs.start()
        try:
            yield
        finally:
            await _app.state.room_turn_jobs.close()
            await _app.state.combat_render_cache.aclose()
            await outbox_dispatcher.close()
            await realtime_hub.close()
            results = await asyncio.gather(
                *(client.aclose() for client in managed_http_clients.values()),
                return_exceptions=True,
            )
            for name, result in zip(managed_http_clients, results, strict=True):
                if isinstance(result, BaseException):
                    logger.error(
                        "outbound HTTP client shutdown failed client=%s error_type=%s",
                        name,
                        type(result).__name__,
                    )
            try:
                await async_engine.dispose()
            except BaseException:
                logger.exception("async database engine shutdown failed")

    app = FastAPI(
        title="SagaSmith Service",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.async_engine = async_engine
    app.state.async_session_factory = make_async_session_factory(async_engine)
    app.state.combat_render_cache = CombatRenderCache(
        max_entries=settings.combat_render_cache_entries,
        max_bytes=settings.combat_render_cache_max_bytes,
        concurrency=settings.combat_render_concurrency,
        ttl_seconds=settings.combat_render_cache_ttl_seconds,
    )
    auth_context_secret = settings.auth_context_secret.get_secret_value()
    app.state.dnd_runtime = dnd_runtime or StreamableHttpDndRuntime(
        settings.dnd_mcp_url,
        auth_context_secret=auth_context_secret,
        http_client=managed_http_client(
            "dnd",
            timeout=httpx.Timeout(30, connect=10),
        ),
    )
    app.state.coc_runtime = coc_runtime or StreamableHttpCocRuntime(
        settings.coc_mcp_url,
        auth_context_secret=auth_context_secret,
        http_client=managed_http_client(
            "coc",
            timeout=httpx.Timeout(30, connect=10),
        ),
    )
    app.state.narrative_runtime = narrative_runtime or HttpNarrativeRuntime(
        settings.agent_api_url,
        settings.agent_api_key.get_secret_value(),
        timeout_seconds=settings.agent_completion_timeout_seconds,
        http_client=managed_http_client(
            "narrative",
            timeout=httpx.Timeout(settings.agent_completion_timeout_seconds, connect=10),
        ),
    )
    app.state.game_runtimes = {
        "dnd5e": app.state.dnd_runtime,
        "coc7e": app.state.coc_runtime,
        "narrative": app.state.narrative_runtime,
    }
    app.state.agent_runtime = agent_runtime or HttpAgentRuntime(
        settings.agent_api_url,
        settings.agent_api_key.get_secret_value(),
        timeout_seconds=settings.agent_completion_timeout_seconds,
        http_client=managed_http_client(
            "agent",
            timeout=httpx.Timeout(settings.agent_completion_timeout_seconds, connect=10),
        ),
    )
    app.state.room_turn_jobs = RoomTurnJobProcessor(
        session_factory,
        lambda job_id: execute_room_turn_job(app, job_id),
        concurrency=settings.room_turn_worker_concurrency,
        poll_seconds=settings.room_turn_worker_poll_seconds,
        lease_seconds=settings.room_turn_worker_lease_seconds,
        reservation_ttl_seconds=settings.agent_reservation_ttl_seconds,
        retry_seconds=settings.room_turn_retry_seconds,
        failure_recorder=record_room_job_failure,
    )
    app.state.outbound_http_clients = managed_http_clients
    app.state.realtime_hub = realtime_hub
    app.state.outbox_dispatcher = outbox_dispatcher
    app.state.rate_limiter = rate_limiter or (
        RedisRateLimiter(settings.redis_url)
        if settings.rate_limit_backend == "redis"
        else MemoryRateLimiter()
    )
    if settings.storage_backend == "s3":
        app.state.private_storage = S3PrivateStorage(
            endpoint=settings.object_endpoint,
            bucket=settings.object_bucket,
            access_key=settings.object_access_key,
            secret_key=settings.object_secret_key.get_secret_value(),
            exchange_root=settings.exchange_dir,
        )
    else:
        app.state.private_storage = LocalPrivateStorage(
            settings.private_storage_dir, settings.exchange_dir
        )

    @app.middleware("http")
    async def hot_path_observability(request: Request, call_next):
        operation_class = hot_path_operation(request.method, request.url.path)
        if operation_class is None:
            return await call_next(request)
        observation = HotPathRequestObservation(operation_class)
        observation_token = bind_hot_path_observation(observation)
        stop = asyncio.Event()
        sampler_started = asyncio.Event()
        sampler = asyncio.create_task(sample_max_event_loop_lag(stop, started=sampler_started))
        await sampler_started.wait()
        outcome = "error"
        try:
            response = await call_next(request)
            outcome = "success" if response.status_code < 400 else "error"
            return response
        finally:
            stop.set()
            event_loop_lag = await sampler
            observe_hot_path_request(
                observation,
                status=outcome,
                event_loop_lag_seconds=event_loop_lag,
            )
            reset_hot_path_observation(observation_token)

    @app.middleware("http")
    async def request_observability(request: Request, call_next):
        supplied = request.headers.get("X-Request-ID", "")
        request_id = supplied if SAFE_REQUEST_ID.fullmatch(supplied) else str(uuid.uuid4())
        started = time.perf_counter()
        request.state.request_id = request_id
        request_token = bind_request_id(request_id)
        try:
            response = None
            if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.cookies.get(
                SESSION_COOKIE
            ):
                origin = request.headers.get("Origin", "")
                expected = urlsplit(settings.public_origin)
                supplied = urlsplit(origin)
                if (supplied.scheme, supplied.netloc) != (expected.scheme, expected.netloc):
                    response = JSONResponse(
                        status_code=403,
                        content={"detail": "same-origin request required"},
                    )
            policy: tuple[str, int, int] | None = None
            path = request.url.path
            if request.method == "POST" and path in {"/api/auth/login", "/api/auth/register"}:
                policy = ("auth", settings.auth_rate_limit, settings.auth_rate_window_seconds)
            elif request.method == "POST" and path.endswith("/messages"):
                policy = ("agent", settings.agent_rate_limit, 60)
            elif request.method == "POST" and path == "/api/packs":
                policy = ("pack", settings.pack_rate_limit, 3600)
            elif request.method == "POST" and path.startswith("/api/modules"):
                policy = ("module", settings.module_rate_limit, 3600)
            elif request.method == "POST" and path == "/api/invites/accept":
                policy = ("invite", 30, 60)
            elif request.method == "POST" and path == "/api/community/posts":
                policy = ("community-post", 30, 60)
            elif request.method == "POST" and path == "/api/community/reports":
                policy = ("community-report", 10, 3600)
            elif request.method == "POST" and path.endswith("/agent-review"):
                policy = ("artifact-review", 10, 3600)
            if response is None and policy is not None:
                category, limit, window_seconds = policy
                client_host = request.client.host if request.client else "unknown"
                identity = request.cookies.get(SESSION_COOKIE) or client_host
                try:
                    retry_after = await app.state.rate_limiter.hit(
                        opaque_rate_key(category, identity),
                        limit=limit,
                        window_seconds=window_seconds,
                    )
                except RateLimiterUnavailableError:
                    response = JSONResponse(
                        status_code=503,
                        content={"detail": "request protection is unavailable"},
                    )
                    retry_after = None
                if retry_after is not None:
                    response = JSONResponse(
                        status_code=429,
                        content={"detail": "rate limit exceeded"},
                        headers={"Retry-After": str(retry_after)},
                    )
            if response is None:
                response = await call_next(request)
        finally:
            reset_request_id(request_token)
        elapsed = time.perf_counter() - started
        route = request.scope.get("route")
        route_path = getattr(route, "path", request.url.path)
        REQUESTS.labels(request.method, route_path, response.status_code).inc()
        HTTP_LATENCY_SECONDS.labels(request.method, route_path).observe(elapsed)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' https: data: blob:; connect-src 'self'; object-src 'none'; "
            "base-uri 'self'; frame-ancestors 'none'"
        )
        logger.info(
            "request method=%s route=%s status=%s duration_ms=%.2f request_id=%s",
            request.method,
            route_path,
            response.status_code,
            elapsed * 1000,
            request_id,
        )
        return response

    app.include_router(auth_router(settings))
    app.include_router(campaign_router)
    app.include_router(community_router)
    app.include_router(identities_router)
    app.include_router(modules_router)
    app.include_router(NOTIFICATION_ROUTER)
    app.include_router(usage_router)
    app.include_router(agent_router)
    app.include_router(rooms_router)
    app.include_router(packs_router)
    app.include_router(invites_router)
    app.include_router(admin_router)
    app.include_router(audit_router)
    app.include_router(operations_router)

    @app.get("/api/health", tags=["operations"])
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    web_root = Path(__file__).parent / "web"
    # Python's Windows MIME registry may not know WebP, which would make the
    # tactical-map texture an opaque download instead of an image response.
    mimetypes.add_type("image/webp", ".webp")
    app.mount("/assets", StaticFiles(directory=web_root), name="web-assets")
    app.mount("/", StaticFiles(directory=web_root, html=True), name="web")

    return app


app = create_app()


def main() -> None:
    uvicorn.run(
        "sagasmith_service.main:app",
        host="0.0.0.0",  # noqa: S104 - container ingress is restricted by deployment policy.
        port=8080,
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
