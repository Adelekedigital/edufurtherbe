"""Liveness and readiness, kept apart on purpose.

**`/health` is liveness** and never touches a dependency: a check that fails
when the database is slow gets a process restarted that was fine, and every
instance restarted at once turns a blip into an outage.

**`/ready` is readiness**: can this process serve traffic now? It asks the
database for one row, briefly, and answers `503` if it cannot. Railway's
deploy healthcheck points here, so a release that cannot reach its database
never receives traffic. It names no host and no error: it is public.
"""

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.deps.core import DatabaseReadyDep

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str


class ReadinessResponse(BaseModel):
    status: str
    checks: dict[str, str]


@router.get("/health")
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": ReadinessResponse,
            "description": "The database did not answer; this process should get no traffic.",
        }
    },
)
async def ready(database: DatabaseReadyDep) -> JSONResponse:
    if not database:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "not ready", "checks": {"database": "unreachable"}},
        )
    return JSONResponse(content={"status": "ready", "checks": {"database": "ok"}})
