"""Deterministically compare repository policy with QStash deployed state."""

from __future__ import annotations

import hashlib
import json
from base64 import b64decode
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.errors import ConfigurationError, UpstreamError
from app.infra.http.upstream import why
from app.infra.jobs.manifest import ResolvedSchedule

TIMEOUT = httpx.Timeout(30.0, connect=10.0)


@dataclass(frozen=True, slots=True)
class ExistingSchedule:
    id: str
    destination: str
    cron: str
    method: str
    body: str
    retries: int
    paused: bool
    labels: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ScheduleChange:
    action: str
    schedule: ResolvedSchedule


def policy_fingerprint(schedule: ResolvedSchedule) -> str:
    """Cover fields QStash's list response does not consistently expose."""
    policy = {
        "body": schedule.canonical_body,
        "cron": schedule.cron,
        "destination": schedule.destination,
        "method": "POST",
        "retries": schedule.retries,
        "timeout": schedule.timeout,
    }
    canonical = json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()[:24]


def _matches(desired: ResolvedSchedule, current: ExistingSchedule) -> bool:
    return (
        current.destination == desired.destination
        and current.cron == desired.cron
        and current.method.upper() == "POST"
        and current.body == desired.canonical_body
        and current.retries == desired.retries
        and not current.paused
        and desired.scope_label in current.labels
        and desired.fingerprint_label in current.labels
    )


def plan_reconciliation(
    desired: list[ResolvedSchedule] | tuple[ResolvedSchedule, ...],
    existing: list[ExistingSchedule] | tuple[ExistingSchedule, ...],
    *,
    environment: str,
) -> tuple[ScheduleChange, ...]:
    """Return a stable diff, touching only this Edufurther environment."""
    wanted = {item.id: item for item in desired}
    present = {item.id: item for item in existing}
    scope = f"edufurther-{environment}-"
    changes: list[ScheduleChange] = []

    for identity, item in wanted.items():
        current = present.get(identity)
        if not item.enabled:
            if current is not None:
                changes.append(ScheduleChange("delete", item))
        elif current is None:
            changes.append(ScheduleChange("create", item))
        elif not _matches(item, current):
            changes.append(ScheduleChange("update", item))

    for identity in present.keys() - wanted.keys():
        if identity.startswith(scope):
            current = present[identity]
            changes.append(
                ScheduleChange(
                    "delete",
                    ResolvedSchedule(
                        id=current.id,
                        name=current.id.removeprefix(scope),
                        destination=current.destination,
                        cron=current.cron,
                        enabled=False,
                        timeout="0s",
                        retries=current.retries,
                        body={},
                    ),
                )
            )

    return tuple(sorted(changes, key=lambda change: change.schedule.id))


def _body(value: Any) -> str:
    """Normalise the list API's plain or base64-encoded body representation."""
    if value is None:
        return ""
    text = str(value)
    if text.startswith("{"):
        return text
    try:
        decoded = b64decode(text, validate=True).decode()
    except ValueError, UnicodeDecodeError:
        return text
    return decoded


class QStashSchedules:
    """The narrow QStash schedule API used by repository reconciliation."""

    def __init__(self, token: str, url: str, client: httpx.Client | None = None) -> None:
        """``url`` is the QStash **origin** — region-scoped, see `Settings.qstash_url`."""
        if not token:
            raise ConfigurationError("QSTASH_TOKEN is required to reconcile schedules")
        self.client = client or httpx.Client(
            base_url=f"{url.rstrip('/')}/v2",
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT,
        )

    def list(self) -> tuple[ExistingSchedule, ...]:
        try:
            response = self.client.get("/schedules")
            response.raise_for_status()
            raw = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise UpstreamError(f"could not list QStash schedules: {why(exc)}") from exc
        if isinstance(raw, dict):
            raw = raw.get("schedules", [])
        if not isinstance(raw, list):
            raise UpstreamError("QStash schedule list was not an array")
        schedules = []
        for item in raw:
            identity = item.get("scheduleId") or item.get("id")
            if not identity:
                continue
            labels = item.get("labels") or []
            schedules.append(
                ExistingSchedule(
                    id=str(identity),
                    destination=str(item.get("destination", "")),
                    cron=str(item.get("cron", "")),
                    method=str(item.get("method", "POST")),
                    body=_body(item.get("body")),
                    retries=int(item.get("retries", 0)),
                    paused=bool(item.get("isPaused", False)),
                    labels=tuple(str(label) for label in labels),
                )
            )
        return tuple(sorted(schedules, key=lambda item: item.id))

    def put(self, schedule: ResolvedSchedule) -> None:
        headers = {
            "Content-Type": "application/json",
            "Upstash-Forward-Content-Type": "application/json",
            "Upstash-Label": f"{schedule.scope_label},{schedule.fingerprint_label}",
            "Upstash-Method": "POST",
            "Upstash-Retries": str(schedule.retries),
            "Upstash-Schedule": schedule.cron,
            "Upstash-Schedule-Id": schedule.id,
            "Upstash-Timeout": schedule.timeout,
        }
        try:
            response = self.client.post(
                f"/schedules/{schedule.destination}",
                content=schedule.canonical_body,
                headers=headers,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise UpstreamError(
                f"could not apply QStash schedule {schedule.id}: {why(exc)}"
            ) from exc

    def delete(self, identity: str) -> None:
        try:
            response = self.client.delete(f"/schedules/{identity}")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise UpstreamError(f"could not delete QStash schedule {identity}: {why(exc)}") from exc


def apply_reconciliation(client: QStashSchedules, changes: tuple[ScheduleChange, ...]) -> None:
    """Apply an already-printed plan in its deterministic order."""
    for change in changes:
        if change.action == "delete":
            client.delete(change.schedule.id)
        else:
            client.put(change.schedule)
