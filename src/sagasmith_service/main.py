from __future__ import annotations

import logging
import re
import time
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from prometheus_client import Counter, Histogram
from sqlalchemy.engine import Engine

from sagasmith_service import __version__
from sagasmith_service.api.admin import router as admin_router
from sagasmith_service.api.agent import router as agent_router
from sagasmith_service.api.auth import router as auth_router
from sagasmith_service.api.campaigns import router as campaign_router
from sagasmith_service.api.invites import router as invites_router
from sagasmith_service.api.operations import router as operations_router
from sagasmith_service.api.packs import router as packs_router
from sagasmith_service.api.usage import router as usage_router
from sagasmith_service.config import Settings, get_settings
from sagasmith_service.database import Base, make_engine, make_session_factory
from sagasmith_service.integrations.agent import AgentRuntime, HttpAgentRuntime
from sagasmith_service.integrations.dnd_mcp import DndRuntime, StreamableHttpDndRuntime
from sagasmith_service.storage import LocalPrivateStorage, S3PrivateStorage

logger = logging.getLogger("sagasmith_service.http")
REQUESTS = Counter(
    "sagasmith_http_requests_total", "HTTP requests", ["method", "route", "status"]
)
LATENCY = Histogram(
    "sagasmith_http_request_seconds", "HTTP request latency", ["method", "route"]
)
SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{8,100}$")


def create_app(
    settings: Settings | None = None,
    engine: Engine | None = None,
    dnd_runtime: DndRuntime | None = None,
    agent_runtime: AgentRuntime | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    engine = engine or make_engine(settings.database_url)
    if settings.env in {"development", "test"}:
        Base.metadata.create_all(engine)
    app = FastAPI(
        title="SagaSmith Service",
        version=__version__,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    app.state.dnd_runtime = dnd_runtime or StreamableHttpDndRuntime(settings.dnd_mcp_url)
    app.state.agent_runtime = agent_runtime or HttpAgentRuntime(
        settings.agent_api_url, settings.agent_api_key.get_secret_value()
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
    async def request_observability(request: Request, call_next):
        supplied = request.headers.get("X-Request-ID", "")
        request_id = supplied if SAFE_REQUEST_ID.fullmatch(supplied) else str(uuid.uuid4())
        started = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - started
        route = request.scope.get("route")
        route_path = getattr(route, "path", request.url.path)
        REQUESTS.labels(request.method, route_path, response.status_code).inc()
        LATENCY.labels(request.method, route_path).observe(elapsed)
        response.headers["X-Request-ID"] = request_id
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
    app.include_router(usage_router)
    app.include_router(agent_router)
    app.include_router(packs_router)
    app.include_router(invites_router)
    app.include_router(admin_router)
    app.include_router(operations_router)

    @app.get("/api/health", tags=["operations"])
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    web_root = Path(__file__).parent / "web"
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
