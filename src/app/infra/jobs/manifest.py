"""Load and resolve the repository-owned QStash schedule policy."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.config import Environment

RUNTIME_JOB_NAMES = frozenset(
    {
        "settle-sessions",
        "credit-reminders",
        "monthly-credits",
        "expire-credits",
        "sync-institutions",
    }
)


class ManifestError(ValueError):
    """The checked-in schedule policy or an override is invalid."""


class RuntimeSchedule(BaseModel):
    """One recurring delivery declared by the repository."""

    model_config = ConfigDict(extra="forbid", strict=True)

    name: str
    handler: str
    cron: str
    environments: dict[Environment, bool]
    timeout: str
    retries: int = Field(ge=0)

    @field_validator("name")
    @classmethod
    def known_job(cls, value: str) -> str:
        if value not in RUNTIME_JOB_NAMES:
            raise ValueError(f"unknown runtime job {value!r}")
        return value

    @field_validator("handler")
    @classmethod
    def exact_handler(cls, value: str) -> str:
        if not value.startswith("/api/v1/internal/jobs/"):
            raise ValueError("handler must be an internal jobs path")
        return value


class RuntimeScheduleManifest(BaseModel):
    """Versioned schedule policy; QStash is only deployed state."""

    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1]
    timezone: Literal["UTC"]
    jobs: list[RuntimeSchedule]

    @model_validator(mode="after")
    def unique_names(self) -> RuntimeScheduleManifest:
        names = [job.name for job in self.jobs]
        if len(names) != len(set(names)):
            raise ManifestError("duplicate runtime schedule name")
        return self


@dataclass(frozen=True, slots=True)
class ResolvedSchedule:
    """A schedule after environment defaults and overrides are applied."""

    id: str
    name: str
    destination: str
    cron: str
    enabled: bool
    timeout: str
    retries: int
    body: dict[str, str]

    @property
    def canonical_body(self) -> str:
        return json.dumps(self.body, sort_keys=True, separators=(",", ":"))

    @property
    def scope_label(self) -> str:
        return self.id.rsplit(f"-{self.name}", 1)[0]

    @property
    def fingerprint_label(self) -> str:
        from app.infra.jobs.reconcile import policy_fingerprint

        return f"policy-{policy_fingerprint(self)}"


def load_manifest(path: Path) -> RuntimeScheduleManifest:
    """Read and strictly validate a manifest."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("jobs"), list):
            names = [job.get("name") for job in raw["jobs"] if isinstance(job, dict)]
            if len(names) != len(set(names)):
                raise ManifestError("duplicate runtime schedule name")
        return RuntimeScheduleManifest.model_validate(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"could not read schedule manifest: {exc}") from exc


def schedule_id(environment: str, name: str) -> str:
    """The operator-visible, environment-qualified QStash identifier."""
    if name not in RUNTIME_JOB_NAMES:
        raise ManifestError(f"unknown job {name!r}")
    if environment not in {"local", "ci", "staging", "production"}:
        raise ManifestError(f"unknown environment {environment!r}")
    return f"edufurther-{environment}-{name}"


def _validated_overrides(
    manifest: RuntimeScheduleManifest, overrides: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    known = {job.name for job in manifest.jobs}
    for name, values in overrides.items():
        if name not in known:
            raise ManifestError(f"unknown job in schedule override: {name}")
        if not isinstance(values, dict):
            raise ManifestError(f"override for {name} must be an object")
        for key, value in values.items():
            if key not in {"cron", "enabled"}:
                raise ManifestError(f"unknown override key {key!r} for {name}")
            if key == "cron" and not isinstance(value, str):
                raise ManifestError(f"cron override for {name} must be a string")
            if key == "enabled" and type(value) is not bool:
                raise ManifestError(f"enabled override for {name} must be a boolean")
    return overrides


def resolve_manifest(
    manifest: RuntimeScheduleManifest,
    *,
    environment: Environment,
    public_base_url: str,
    overrides: dict[str, Any],
) -> tuple[ResolvedSchedule, ...]:
    """Resolve one environment without mutating the checked-in policy."""
    checked = _validated_overrides(manifest, overrides)
    base = public_base_url.rstrip("/")
    if not base.startswith(("https://", "http://")):
        raise ManifestError("public base URL must be absolute")
    resolved = []
    for job in manifest.jobs:
        override = checked.get(job.name, {})
        identity = schedule_id(environment, job.name)
        resolved.append(
            ResolvedSchedule(
                id=identity,
                name=job.name,
                destination=f"{base}{job.handler}",
                cron=override.get("cron", job.cron),
                enabled=override.get("enabled", job.environments[environment]),
                timeout=job.timeout,
                retries=job.retries,
                body={"job_id": identity},
            )
        )
    return tuple(sorted(resolved, key=lambda item: item.id))
