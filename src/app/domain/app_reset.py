"""Clearing rehearsal app data before the real load: the rules, with no I/O.

Production held a dress run (the 8 Nov legacy export, loaded 14 Nov) that must
be cleared before the final export is loaded (cutover page, "Reset script for
production app data"). ``scripts/reset_app_data.py`` wires it up and
``infra/etl/reset.py`` reads and deletes; what counts as *real* data, which
database is the intended one, and the order rows can be deleted in are decided
here, so each is one rule with unit tests.

**Real data refuses the reset, and nothing overrides that.** Two signals, the
pattern for a destructive maintenance tool (count what the tool would destroy
that it cannot recreate, and stop):

* an account that did not come from the legacy app (``signup_source`` is not
  ``migrated``) and is not one of ours (``is_internal``: a live admin grant or a
  demo address, decision 329). That is a sign-up on the new app;
* a row created at or after ``real_data_from`` in a table that holds activity.
  The default is the start of the final load window, so the real load itself
  (its credit lots and ledger rows are stamped at load time) and every app
  write after it refuse. Before it, a rehearsal can be cleared as often as
  needed. ``--real-data-from`` may only move it **earlier**: a later cutoff
  would switch the check off, so moving the window is a reviewed code change.

Counts and table names only: never a person, an address or the DSN.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from graphlib import CycleError, TopologicalSorter

#: The start of the final load window (Thu 19 Nov 2026, UTC): a row created at
#: or after it is real data. The flag may name an earlier instant, never a later.
DEFAULT_REAL_DATA_FROM = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)

#: A Supabase project ref: what the direct host (``db.<ref>.supabase.co``) and
#: the pooler's user name (``postgres.<ref>``) both carry.
_REF = r"[a-z0-9]{20}"
_DIRECT_HOST = re.compile(rf"^db\.({_REF})\.supabase\.co$")
_POOLER_USER = re.compile(rf"^postgres\.({_REF})$")
#: Hosts that are Supabase's, where the database is always called ``postgres``.
_SUPABASE_HOST = re.compile(r"(^|\.)supabase\.(co|com)$")


@dataclass(frozen=True)
class Target:
    """Where a DSN points, without its password."""

    host: str | None
    database: str | None
    user: str | None

    @property
    def project_ref(self) -> str | None:
        """The Supabase project, read from the host or the pooler's user."""
        for pattern, value in ((_DIRECT_HOST, self.host), (_POOLER_USER, self.user)):
            if value and (found := pattern.match(value)):
                return found.group(1)
        return None

    @property
    def on_supabase(self) -> bool:
        return bool(self.host and _SUPABASE_HOST.search(self.host))

    def describe(self) -> str:
        ref = self.project_ref or "-"
        return f"host={self.host or '-'} database={self.database or '-'} project={ref}"


def confirms(target: Target, given: str) -> bool:
    """Whether ``--confirm-database`` names this target.

    A Supabase database is always called ``postgres``, so on Supabase only the
    project ref identifies it, and a Supabase DSN whose ref cannot be read
    confirms nothing; elsewhere the database name does.
    """
    supabase = target.on_supabase or target.project_ref is not None
    expected = target.project_ref if supabase else target.database
    return bool(expected) and given.strip() == expected


def cutoff_problem(real_data_from: dt.datetime) -> str | None:
    """Why a ``--real-data-from`` is refused, or ``None``.

    Naive is ambiguous, and later than the default would let rows the real load
    or real people wrote pass as rehearsal.
    """
    if real_data_from.tzinfo is None:
        return "--real-data-from needs a zone"
    if real_data_from > DEFAULT_REAL_DATA_FROM:
        return f"--real-data-from may not be later than {DEFAULT_REAL_DATA_FROM.isoformat()}"
    return None


def deletion_order(tables: Iterable[str], references: Iterable[tuple[str, str]]) -> tuple[str, ...]:
    """Every table after the tables that reference it, so a ``RESTRICT`` key
    never meets a parent deleted before its child.

    ``references`` holds ``(child, parent)`` pairs; pairs outside ``tables`` and
    self-references are ignored (one ``DELETE`` removes both ends). Ties are
    broken by name, so the order is the same on every run. Raises
    ``CycleError`` when no order exists.
    """
    members = sorted(set(tables))
    graph: dict[str, set[str]] = {table: set() for table in members}
    for child, parent in references:
        if child != parent and child in graph and parent in graph:
            # A parent waits for its children.
            graph[parent].add(child)
    sorter = TopologicalSorter(graph)
    sorter.prepare()
    order: list[str] = []
    while sorter.is_active():
        ready = sorted(sorter.get_ready())
        order.extend(ready)
        sorter.done(*ready)
    return tuple(order)


@dataclass(frozen=True)
class Survey:
    """What the database holds, read before anything is deleted."""

    real_data_from: dt.datetime
    #: Rows per table that is cleared whole.
    clear: Mapping[str, int]
    #: Rows per kept table (catalogue tables: the rows that stay).
    keep: Mapping[str, int]
    #: User-created rows in the catalogue tables, which are cleared.
    user_created: Mapping[str, int]
    #: Accounts that signed up on the new app, ours excluded.
    new_accounts: int
    #: Our own accounts (admins, demo) that signed up on the new app.
    internal_accounts: int
    #: Rows created at or after ``real_data_from``, per activity table.
    recent: Mapping[str, int]
    #: Tables in the database that are neither kept nor cleared.
    unknown_tables: tuple[str, ...] = ()
    #: Foreign keys through which a delete could reach a kept row.
    unsafe_keys: tuple[str, ...] = ()
    #: Kept catalogue rows pointing at a row that would be deleted, per
    #: ``table.column`` (an approver, or a merge into a user-created row).
    blocked: Mapping[str, int] = field(default_factory=dict)
    #: Set when the cleared tables reference each other in a cycle.
    cycle: str | None = None
    #: Private files (intake, message) whose rows are cleared.
    stored_files: int = 0
    #: Whether those files' objects can be removed from storage.
    storage_ready: bool = True
    #: Cleared activity tables with no ``created_at``, so not checked by time.
    untimed: tuple[str, ...] = ()


def refusals(survey: Survey) -> tuple[str, ...]:
    """Every reason not to delete, empty when it is safe. Never a person."""
    found: list[str] = []
    if survey.new_accounts:
        found.append(
            f"{survey.new_accounts} account(s) signed up on the new app "
            "(signup_source is not 'migrated'): real users"
        )
    when = survey.real_data_from.isoformat()
    found.extend(
        f"{table}: {count} row(s) created at or after {when}"
        for table, count in sorted(survey.recent.items())
        if count
    )
    if survey.unknown_tables:
        found.append(
            "tables neither kept nor cleared (classify them in infra/etl/reset.py): "
            + ", ".join(survey.unknown_tables)
        )
    found.extend(f"a delete could reach a kept row through {key}" for key in survey.unsafe_keys)
    found.extend(
        f"{key}: {count} kept row(s) point at a row the reset would delete"
        for key, count in sorted(survey.blocked.items())
        if count
    )
    if survey.cycle:
        found.append(f"the cleared tables reference each other in a cycle: {survey.cycle}")
    if survey.stored_files and not survey.storage_ready:
        found.append(
            f"{survey.stored_files} private file(s) would be left in storage with nothing "
            "pointing at them: configure the intake bucket (SUPABASE_INTAKE_BUCKET)"
        )
    return tuple(found)


def report(survey: Survey, *, applied: bool) -> str:
    """The operator's summary: table names and counts only."""
    lines = [f"real data from: {survey.real_data_from.isoformat()}", "", "KEEP (untouched):"]
    lines.extend(f"  {table:<42} {count:>8}" for table, count in sorted(survey.keep.items()))
    verb = "deleted" if applied else "would delete"
    lines += ["", f"CLEAR ({verb}):"]
    lines.extend(f"  {table:<42} {count:>8}" for table, count in sorted(survey.clear.items()))
    lines.extend(
        f"  {table + ' (user-created rows)':<42} {count:>8}"
        for table, count in sorted(survey.user_created.items())
    )
    total = sum(survey.clear.values()) + sum(survey.user_created.values())
    lines.append(f"  {'total':<42} {total:>8}")
    if survey.stored_files:
        lines.append(f"\n{survey.stored_files} private file(s) are removed from storage too")
    admins = survey.clear.get("admin_users", 0)
    wording = survey.clear.get("email_line_overrides", 0)
    if admins or wording:
        lines.append(
            f"\nadmin configuration is cleared: {admins} admin grant(s), {wording} reworded "
            "email line(s). Grants come back with the legacy load or provision_auth.py "
            "--grant-admin; reworded lines must be entered again"
        )
    if survey.untimed:
        lines.append("\nnot checked by time (no created_at): " + ", ".join(survey.untimed))
    if survey.internal_accounts:
        lines.append(
            f"\n{survey.internal_accounts} internal account(s) (admin or demo) signed up "
            "on the new app; they are cleared with the rest"
        )
    return "\n".join(lines)


__all__ = [
    "DEFAULT_REAL_DATA_FROM",
    "CycleError",
    "Survey",
    "Target",
    "confirms",
    "cutoff_problem",
    "deletion_order",
    "refusals",
    "report",
]
