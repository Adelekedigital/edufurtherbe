"""Liveness and readiness, kept apart on purpose.

**`/health` is liveness** and never touches a dependency: a check that fails
when the database is slow gets a process restarted that was fine, and every
instance restarted at once turns a blip into an outage.

**`/ready` is readiness**: can this process serve traffic now? It asks the
database for one row, briefly, and answers `503` if it cannot. It is the path
for a deploy healthcheck, so a release that cannot reach its database gets no
traffic. It names no host and no error: it is public.
"""

from typing import Literal

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.api.deps.core import DatabaseReadyDep

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not ready"]
    checks: dict[str, Literal["ok", "unreachable"]]


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
    # Built through the model, so the body cannot drift from the published shape.
    body = ReadinessResponse(
        status="ready" if database else "not ready",
        checks={"database": "ok" if database else "unreachable"},
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK if database else status.HTTP_503_SERVICE_UNAVAILABLE,
        content=body.model_dump(),
    )
