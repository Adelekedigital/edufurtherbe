"""Liveness endpoint.

Deliberately does not touch the database. A health check that fails when a
dependency is slow causes the orchestrator to restart a process that was fine —
readiness is a separate concern, added when there is a dependency to report on.
"""

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str


@router.get("/health")
def health() -> HealthResponse:
    return HealthResponse(status="ok")
