from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Request, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

router = APIRouter(tags=["operations"])


@router.get("/api/product")
def product_configuration(request: Request, response: Response) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    return {"enabled_systems": sorted(request.app.state.settings.enabled_systems)}


@router.get("/api/ready")
async def readiness(request: Request, response: Response) -> dict[str, object]:
    state = request.app.state
    if not hasattr(state, "readiness_lock"):
        state.readiness_lock = asyncio.Lock()
    async with state.readiness_lock:
        cached = getattr(state, "readiness_cache", None)
        if cached is None or cached[0] <= time.monotonic():
            result = await _probe_readiness(request)
            state.readiness_cache = (
                time.monotonic() + state.settings.readiness_cache_seconds, result
            )
        else:
            result = cached[1]
    if result["status"] != "ready":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    response.headers["Cache-Control"] = "no-store"
    if state.settings.env == "production":
        return {"status": result["status"]}
    return result


async def _probe_readiness(request: Request) -> dict[str, object]:
    components: dict[str, str] = {}
    try:
        with request.app.state.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        components["database"] = "not_ready"
    else:
        components["database"] = "ready"

    probes = {
        **{
            {"dnd5e": "dnd_mcp", "coc7e": "coc_mcp", "narrative": "narrative_mcp"}[name]:
                runtime.probe()
            for name, runtime in request.app.state.game_runtimes.items()
        },
        "agent": request.app.state.agent_runtime.probe(),
        "rate_limiter": request.app.state.rate_limiter.probe(),
    }
    results = await asyncio.gather(*probes.values(), return_exceptions=True)
    for name, result in zip(probes, results, strict=True):
        components[name] = "not_ready" if isinstance(result, BaseException) else "ready"
    try:
        request.app.state.private_storage.probe()
    except Exception:
        components["private_storage"] = "not_ready"
    else:
        components["private_storage"] = "ready"

    ready = all(value == "ready" for value in components.values())
    return {"status": "ready" if ready else "not_ready", "components": components}


@router.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
