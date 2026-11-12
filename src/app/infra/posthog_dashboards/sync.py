"""Make PostHog match `definitions` (decision 341): the dashboards and their
insights, matched by `MANAGED_TAG` plus the ``managed-key`` line in their
description, so a rename updates in place and nothing is duplicated. The repo
is the source of truth: with ``apply`` every managed item is written again,
hand edits included. A dry run reads and plans only.

Internal accounts are left out of every insight by the ids listed in it, read
on each run: a run after an account becomes internal (or stops) refreshes them.
A managed insight no longer defined leaves the managed dashboards; it is not deleted.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from app.core.errors import UpstreamError
from app.infra.clients.posthog_admin import PostHogAdmin
from app.infra.posthog_dashboards.definitions import (
    DASHBOARDS,
    MANAGED_TAG,
    Dashboard,
    insight_query,
)

Kind = Literal["dashboard", "insight"]
Action = Literal["create", "update", "remove"]

INSIGHT_NOTE = "Defined in the backend repo; edits here are overwritten by the next sync."
_KEY_LINE = re.compile(r"^managed-key: (\S+)$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Change:
    kind: Kind
    name: str
    action: Action


@dataclass(slots=True)
class Plan:
    changes: list[Change] = field(default_factory=list)
    #: The internal accounts each insight leaves out.
    internal: int = 0


def described(text: str, key: str) -> str:
    return f"{text}\n\nmanaged-key: {key}"


def managed_key(row: dict[str, Any]) -> str | None:
    """The definition ``row`` is, when the sync made it (tagged, not deleted)."""
    if MANAGED_TAG not in (row.get("tags") or []) or row.get("deleted"):
        return None
    found = _KEY_LINE.search(row.get("description") or "")
    return found.group(1) if found else None


def _by_key(rows: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """The first managed row per key, oldest first, and every other managed row."""
    first: dict[str, dict[str, Any]] = {}
    extra = []
    for row in sorted(rows, key=lambda r: int(r["id"])):
        key = managed_key(row)
        if key is None:
            continue
        if key in first:
            extra.append(row)
        else:
            first[key] = row
    return first, extra


def _created(
    row: dict[str, Any], name: str, undo: Callable[[int, dict[str, Any]], Any]
) -> dict[str, Any]:
    """A created item, kept only if it kept the tag: without it the next run
    could not find it and would make another, so it is deleted (PostHog
    deletes by `deleted`) and the run stops."""
    if MANAGED_TAG not in (row.get("tags") or []):
        undo(int(row["id"]), {"deleted": True})
        raise UpstreamError(f"PostHog did not keep the {MANAGED_TAG} tag on {name!r}; deleted it")
    return row


def _dashboard(
    admin: PostHogAdmin,
    dashboard: Dashboard,
    found: dict[str, Any] | None,
    apply: bool,
    plan: Plan,
) -> int | None:
    body = {
        "name": dashboard.name,
        "description": described(dashboard.description, dashboard.key),
        "tags": [MANAGED_TAG],
    }
    plan.changes.append(
        Change("dashboard", dashboard.name, "create" if found is None else "update")
    )
    if not apply:
        return None if found is None else int(found["id"])
    if found is None:
        made = _created(admin.create_dashboard(body), dashboard.name, admin.update_dashboard)
        return int(made["id"])
    admin.update_dashboard(int(found["id"]), body)
    return int(found["id"])


def _insights(
    admin: PostHogAdmin,
    internal: Sequence[UUID],
    placed: dict[str, int | None],
    apply: bool,
    plan: Plan,
) -> None:
    found, extra = _by_key(admin.insights())
    ours = {i for i in placed.values() if i is not None}
    for dashboard in DASHBOARDS:
        target = placed[dashboard.key]
        for insight in dashboard.insights:
            body: dict[str, Any] = {
                "name": insight.name,
                "query": insight_query(insight, internal),
                "description": described(INSIGHT_NOTE, insight.key),
                "tags": [MANAGED_TAG],
            }
            row = found.pop(insight.key, None)
            plan.changes.append(
                Change("insight", insight.name, "create" if row is None else "update")
            )
            if not apply or target is None:
                continue
            if row is None:
                made = admin.create_insight({**body, "dashboards": [target]})
                _created(made, insight.name, admin.update_insight)
            else:
                current = set(row.get("dashboards") or [])
                on = sorted((current - ours) | {target})
                admin.update_insight(int(row["id"]), {**body, "dashboards": on})
    for row in [*found.values(), *extra]:
        current = set(row.get("dashboards") or [])
        if current & ours:
            plan.changes.append(Change("insight", str(row.get("name")), "remove"))
            if apply:
                admin.update_insight(int(row["id"]), {"dashboards": sorted(current - ours)})


def sync(admin: PostHogAdmin, internal: Sequence[UUID], *, apply: bool) -> Plan:
    """Plan, and with ``apply`` make, every change. In a dry run a dashboard
    not created yet has no id, so its insights are planned against none."""
    listed = sorted(internal, key=str)
    plan = Plan(internal=len(listed))
    dashboards, _ = _by_key(admin.dashboards())
    placed = {d.key: _dashboard(admin, d, dashboards.get(d.key), apply, plan) for d in DASHBOARDS}
    _insights(admin, listed, placed, apply, plan)
    return plan
