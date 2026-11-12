"""Make PostHog match `definitions` (decision 341): the internal cohort, the
dashboards and their insights, matched by name plus `MANAGED_TAG`, updated in
place and never duplicated. A dry run reads and plans; only ``apply`` writes.

An unchanged item is one whose description carries the same stamp, a hash of
what was sent, so PostHog filling in defaults on a stored query is no change.
A managed insight no longer defined leaves the managed dashboards; it is not deleted.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from app.core.errors import UpstreamError
from app.infra.clients.posthog_admin import PostHogAdmin
from app.infra.posthog_dashboards.definitions import (
    COHORT_DESCRIPTION,
    COHORT_NAME,
    DASHBOARDS,
    MANAGED_TAG,
    Dashboard,
    insight_query,
)

Kind = Literal["cohort", "dashboard", "insight"]
Action = Literal["create", "update", "unchanged", "remove"]

INSIGHT_NOTE = "Defined in the backend repo; edits here are overwritten by the next sync."


@dataclass(frozen=True, slots=True)
class Change:
    kind: Kind
    name: str
    action: Action


@dataclass(slots=True)
class Plan:
    changes: list[Change] = field(default_factory=list)
    #: Internal accounts, and those PostHog has no person for yet.
    internal: int = 0
    not_in_posthog: int = 0


def managed(row: dict[str, Any]) -> bool:
    return MANAGED_TAG in (row.get("tags") or []) and not row.get("deleted")


def _stamp(text: str, sent: dict[str, Any]) -> str:
    digest = hashlib.sha256(json.dumps(sent, sort_keys=True).encode()).hexdigest()[:12]
    return f"{text} [{MANAGED_TAG} {digest}]"


def _tagged(row: dict[str, Any], name: str) -> dict[str, Any]:
    """A created item, refused unless it kept the tag: without it the next run
    cannot find it and would make another."""
    if not managed(row):
        raise UpstreamError(f"PostHog did not keep the {MANAGED_TAG} tag on {name!r}")
    return row


def _cohort(admin: PostHogAdmin, internal: Sequence[UUID], apply: bool, plan: Plan) -> int | None:
    """Sync the cohort's members to ``internal`` (distinct ids), returning its id."""
    found = next(
        (
            row
            for row in sorted(admin.cohorts(), key=lambda r: int(r["id"]))
            if row.get("name") == COHORT_NAME
            and MANAGED_TAG in (row.get("description") or "")
            and not row.get("deleted")
        ),
        None,
    )
    wanted = {str(user) for user in internal}
    covered: set[str] = set()
    remove: list[str] = []
    for person in admin.members(int(found["id"])) if found else []:
        ids = {str(d) for d in person.get("distinct_ids") or []} & wanted
        if ids:
            covered |= ids
        else:
            remove.append(str(person["uuid"]))
    add: list[str] = []
    for distinct_id in sorted(wanted - covered):
        uuid = admin.person_uuid(distinct_id)
        if uuid is None:
            plan.not_in_posthog += 1
        elif uuid not in add:
            add.append(uuid)
    plan.internal = len(wanted)
    action: Action = "create" if found is None else "update" if add or remove else "unchanged"
    plan.changes.append(Change("cohort", COHORT_NAME, action))
    if not apply:
        return int(found["id"]) if found else None
    if found is None:
        found = admin.create_cohort(
            {"name": COHORT_NAME, "description": COHORT_DESCRIPTION, "is_static": True}
        )
    cohort_id = int(found["id"])
    if add:
        admin.add_members(cohort_id, add)
    for person_uuid in remove:
        admin.remove_member(cohort_id, person_uuid)
    return cohort_id


def _dashboard(
    admin: PostHogAdmin, dashboard: Dashboard, rows: list[dict[str, Any]], apply: bool, plan: Plan
) -> int | None:
    body = {
        "name": dashboard.name,
        "description": _stamp(dashboard.description, {"name": dashboard.name}),
        "tags": [MANAGED_TAG],
    }
    found = next((row for row in rows if row.get("name") == dashboard.name), None)
    if found is None:
        plan.changes.append(Change("dashboard", dashboard.name, "create"))
        if not apply:
            return None
        return int(_tagged(admin.create_dashboard(body), dashboard.name)["id"])
    same = found.get("description") == body["description"]
    plan.changes.append(Change("dashboard", dashboard.name, "unchanged" if same else "update"))
    if apply and not same:
        admin.update_dashboard(int(found["id"]), body)
    return int(found["id"])


def _insights(
    admin: PostHogAdmin, cohort_id: int, placed: dict[str, int | None], apply: bool, plan: Plan
) -> None:
    rows = sorted((r for r in admin.insights() if managed(r)), key=lambda r: int(r["id"]))
    by_name: dict[str, dict[str, Any]] = {}
    leftover = []
    for row in rows:
        if row.get("name") in by_name:
            leftover.append(row)
        else:
            by_name[str(row.get("name"))] = row
    ours = {i for i in placed.values() if i is not None}
    for dashboard in DASHBOARDS:
        target = placed[dashboard.name]
        for insight in dashboard.insights:
            query = insight_query(insight, cohort_id)
            body: dict[str, Any] = {
                "name": insight.name,
                "query": query,
                "description": _stamp(INSIGHT_NOTE, {"name": insight.name, "query": query}),
                "tags": [MANAGED_TAG],
            }
            found = by_name.pop(insight.name, None)
            if found is None:
                plan.changes.append(Change("insight", insight.name, "create"))
                if apply and target is not None:
                    _tagged(admin.create_insight({**body, "dashboards": [target]}), insight.name)
                continue
            current = set(found.get("dashboards") or [])
            on = sorted((current - ours) | ({target} if target is not None else set()))
            same = found.get("description") == body["description"] and set(on) == current
            plan.changes.append(Change("insight", insight.name, "unchanged" if same else "update"))
            if apply and not same:
                admin.update_insight(int(found["id"]), {**body, "dashboards": on})
    for row in [*by_name.values(), *leftover]:
        current = set(row.get("dashboards") or [])
        if current & ours:
            plan.changes.append(Change("insight", str(row.get("name")), "remove"))
            if apply:
                admin.update_insight(int(row["id"]), {"dashboards": sorted(current - ours)})


def sync(admin: PostHogAdmin, internal: Sequence[UUID], *, apply: bool) -> Plan:
    """Plan, and with ``apply`` make, every change. In a dry run an item not
    created yet has no id, so what depends on it is planned against none."""
    plan = Plan()
    cohort_id = _cohort(admin, internal, apply, plan)
    rows = sorted((r for r in admin.dashboards() if managed(r)), key=lambda r: int(r["id"]))
    placed = {d.name: _dashboard(admin, d, rows, apply, plan) for d in DASHBOARDS}
    _insights(admin, cohort_id or 0, placed, apply, plan)
    return plan
