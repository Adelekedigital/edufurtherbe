"""Signed machine endpoints for repository-declared runtime jobs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.api.deps import RuntimeJobDep

router = APIRouter(prefix="/api/v1/internal/jobs", tags=["internal-jobs"])


@router.post("/{job_name}")
async def run_runtime_job(result: RuntimeJobDep) -> dict[str, Any]:
    """Return the terminal machine-readable result from the shared runner."""
    return result
