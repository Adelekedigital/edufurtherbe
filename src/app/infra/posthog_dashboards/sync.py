"""Make PostHog match `definitions` (decision 341): the dashboards and their
insights, matched by `MANAGED_TAG` plus the ``managed-key`` line in their
description, so a rename updates in place and nothing is duplicated. The repo
is the source of truth: with ``apply`` every managed item that differs from
its definition is rewritten, hand edits included, one that already matches is
left alone (``unchanged``, decision 344), and a duplicate or one no longer
defined is deleted (PostHog deletes by `deleted`). A dry run reads and plans
only, the same plan.

Internal accounts are left out of every insight by the ids listed in it, read
on each run: a run after an account becomes internal (or stops) refreshes them.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from app.core.errors import UpstreamError
from app.infra.clients.posthog_admin import PostHogAdmin
from app.infra.posthog_dashboards.definitions import DASHBOARDS, MANAGED_TAG, insight_query

Kind = Literal["dashboard", "insight"]
Action = Literal["create", "rewrite", "delete", "unchanged"]

INSIGHT_NOTE = "Defined in the backend repo; edits here are overwritten by the next sync."
_KEY_LINE = re.compile(r"^managed-key: (\S+)\s*$", re.MULTILINE)
_GONE = {"deleted": True}
_COUNTED: dict[Action, str] = {
    "create": "created",
    "rewrite": "rewritten",
    "delete": "deleted",
    "unchanged": "unchanged",
}
#: Counts that describe a run rather than count its writes: a run with only
#: these is a no-op.
NOT_WORK = frozenset({"unchanged", "internal"})

Write = Callable[[int, dict[str, Any]], Any]


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

    def counts(self) -> dict[str, int]:
        """What a job run reports (decision 344): planned changes per action,
        what a dry run would do, and how many ids each insight lists."""
        done = dict.fromkeys(_COUNTED.values(), 0)
        for change in self.changes:
            done[_COUNTED[change.action]] += 1
        return {**done, "internal": self.internal}


def described(text: str, key: str) -> str:
    return f"{text}\n\nmanaged-key: {key}"


def managed_key(row: dict[str, Any]) -> str | None:
    """The definition ``row`` is, when the sync made it (tagged, not deleted);
    a description stored with Windows line ends reads the same."""
    if MANAGED_TAG not in (row.get("tags") or []) or row.get("deleted"):
        return None
    found = _KEY_LINE.search(str(row.get("description") or ""))
    return found.group(1) if found else None


def _normal(field_name: str, value: Any) -> Any:
    """A field as compared: lists as sets of their items (PostHog keeps no
    order we rely on), text with Unix line ends (as `managed_key` reads it)."""
    if field_name in {"tags", "dashboards"}:
        return sorted(value or [], key=str)
    if isinstance(value, str):
        return value.replace("\r\n", "\n")
    return value


def _same(row: dict[str, Any], body: dict[str, Any]) -> bool:
    """``row`` already reads as ``body`` would write it: every field sent."""
    return all(_normal(k, row.get(k)) == _normal(k, v) for k, v in body.items())


def _by_key(rows: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """The oldest managed row per key, and every other managed row."""
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


def _created(row: dict[str, Any], name: str, key: str, undo: Write) -> dict[str, Any]:
    """A created item, kept only if it reads back as ``key``: otherwise the
    next run could not find it and would make another, so it is deleted and
    the run stops."""
    if managed_key(row) != key:
        undo(int(row["id"]), _GONE)
        raise UpstreamError(
            f"PostHog did not keep the {MANAGED_TAG} tag or key on {name!r}; deleted it"
        )
    return row


def _delete(rows: list[dict[str, Any]], kind: Kind, write: Write, apply: bool, plan: Plan) -> None:
    for row in rows:
        plan.changes.append(Change(kind, str(row.get("name")), "delete"))
        if apply:
            write(int(row["id"]), _GONE)


def _dashboards(admin: PostHogAdmin, apply: bool, plan: Plan) -> dict[str, int | None]:
    """Each dashboard's id (``None`` in a dry run before it exists)."""
    found, extra = _by_key(admin.dashboards())
    placed: dict[str, int | None] = {}
    for dashboard in DASHBOARDS:
        body = {
            "name": dashboard.name,
            "description": described(dashboard.description, dashboard.key),
            "tags": [MANAGED_TAG],
        }
        row = found.pop(dashboard.key, None)
        action: Action = "create" if row is None else "unchanged" if _same(row, body) else "rewrite"
        plan.changes.append(Change("dashboard", dashboard.name, action))
        if not apply:
            placed[dashboard.key] = None if row is None else int(row["id"])
        elif row is None:
            made = admin.create_dashboard(body)
            placed[dashboard.key] = int(
                _created(made, dashboard.name, dashboard.key, admin.update_dashboard)["id"]
            )
        else:
            if action == "rewrite":
                admin.update_dashboard(int(row["id"]), body)
            placed[dashboard.key] = int(row["id"])
    _delete([*found.values(), *extra], "dashboard", admin.update_dashboard, apply, plan)
    return placed


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
            if row is None:
                plan.changes.append(Change("insight", insight.name, "create"))
                if apply and target is not None:
                    made = admin.create_insight({**body, "dashboards": [target]})
                    _created(made, insight.name, insight.key, admin.update_insight)
                continue
            # A dashboard a dry run has yet to create is a change of placement.
            current = set(row.get("dashboards") or [])
            on = sorted((current - ours) | ({target} if target is not None else set()))
            body["dashboards"] = on
            same = target is not None and _same(row, body)
            plan.changes.append(Change("insight", insight.name, "unchanged" if same else "rewrite"))
            if apply and not same and target is not None:
                admin.update_insight(int(row["id"]), body)
    _delete([*found.values(), *extra], "insight", admin.update_insight, apply, plan)


def sync(admin: PostHogAdmin, internal: Sequence[UUID], *, apply: bool) -> Plan:
    """Plan, and with ``apply`` make, every change: the dashboards first, so
    a dry run plans exactly what an apply does."""
    listed = sorted(internal, key=str)
    plan = Plan(internal=len(listed))
    placed = _dashboards(admin, apply, plan)
    _insights(admin, listed, placed, apply, plan)
    return plan
