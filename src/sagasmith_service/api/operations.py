from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

router = APIRouter(tags=["operations"])


@router.get("/api/ready")
async def readiness(request: Request, response: Response) -> dict[str, object]:
    components: dict[str, str] = {}
    try:
        with request.app.state.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        components["database"] = "not_ready"
    else:
        components["database"] = "ready"

    probes = {
        "dnd_mcp": request.app.state.dnd_runtime.probe(),
        "coc_mcp": request.app.state.coc_runtime.probe(),
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
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if ready else "not_ready", "components": components}


@router.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
