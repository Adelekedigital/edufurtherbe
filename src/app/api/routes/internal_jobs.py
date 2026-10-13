"""Signed machine endpoints for repository-declared runtime jobs."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import RuntimeJobDep
from app.api.schemas.jobs import RuntimeJobRead

router = APIRouter(prefix="/api/v1/internal/jobs", tags=["internal-jobs"])


@router.post("/{job_name}", response_model=RuntimeJobRead)
async def run_runtime_job(result: RuntimeJobDep) -> RuntimeJobRead:
    """Return the terminal machine-readable result from the shared runner."""
    return RuntimeJobRead.model_validate(result)
