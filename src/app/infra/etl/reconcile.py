"""Did the load actually do what it claimed?

The runbook's rule: a phase is not done when it loads without error, it is done
when reconciliation passes. Loading without error is a low bar — every check the
schema enforces has already run by then, so what is left is exactly the class of
defect the schema cannot see.

Three checks, and the third is the one worth having:

1. **Counts match.** Cheapest, weakest. Catches a truncated read.
2. **Every source row landed.** Catches a silently skipped record, which a count
   would miss if anything else was double-written.
3. **Timestamps equal the source.** The only check that can detect a load which
   ran with ``trg_set_updated_at`` still enabled — where every row is present,
   every count agrees, every column is populated, and every ``updated_at`` is
   the import clock.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain.transform import UserRow
from app.domain.transform.profiles import ProfilePlan
from app.infra.etl.profiles import ProfileCounts


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """What the database says, against what was meant to go in."""

    expected: int
    loaded: int
    missing: tuple[str, ...] = ()
    wrong_created_at: tuple[str, ...] = ()
    wrong_updated_at: tuple[str, ...] = ()
    null_email: int = 0
    null_first_name: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def report(self) -> str:
        lines = [
            f"expected {self.expected}, loaded {self.loaded}",
            f"missing: {len(self.missing)}",
            f"created_at differs from source: {len(self.wrong_created_at)}",
            f"updated_at differs from source: {len(self.wrong_updated_at)}",
            f"null email: {self.null_email}   null first_name: {self.null_first_name}",
        ]
        lines += [f"FAIL {problem}" for problem in self.problems]
        return "\n".join(lines)


async def reconcile_users(connection: AsyncConnection, rows: Sequence[UserRow]) -> Reconciliation:
    """Compare the loaded ``users`` against the rows that were meant to land."""
    expected = {row.legacy_bubble_id: row for row in rows}

    result = await connection.execute(
        text(
            "SELECT legacy_bubble_id, created_at, updated_at, email, first_name "
            "FROM users WHERE legacy_bubble_id IS NOT NULL"
        )
    )
    loaded = {r.legacy_bubble_id: r for r in result}

    missing = tuple(sorted(set(expected) - set(loaded)))

    # Compared to the microsecond. A load that ran with the trigger enabled
    # produces `updated_at` values within a second or two of each other and of
    # now(), so a tolerant comparison would pass exactly the failure this exists
    # to catch.
    wrong_created = tuple(
        sorted(
            key
            for key, row in expected.items()
            if key in loaded and loaded[key].created_at != row.created_at
        )
    )
    wrong_updated = tuple(
        sorted(
            key
            for key, row in expected.items()
            if key in loaded and loaded[key].updated_at != row.updated_at
        )
    )

    null_email = sum(1 for row in loaded.values() if not row.email)
    null_first_name = sum(1 for row in loaded.values() if not row.first_name)

    problems: list[str] = []
    if len(loaded) != len(expected):
        problems.append(f"count mismatch: expected {len(expected)}, found {len(loaded)}")
    if missing:
        problems.append(f"{len(missing)} source rows did not land, first: {missing[:5]}")
    if wrong_created:
        problems.append(f"{len(wrong_created)} rows have a created_at the source did not supply")
    if wrong_updated:
        problems.append(
            f"{len(wrong_updated)} rows have an updated_at the source did not supply — "
            f"the load probably ran with {'trg_set_updated_at'} enabled"
        )
    if null_email:
        problems.append(f"{null_email} rows have no email")

    return Reconciliation(
        expected=len(expected),
        loaded=len(loaded),
        missing=missing,
        wrong_created_at=wrong_created,
        wrong_updated_at=wrong_updated,
        null_email=null_email,
        null_first_name=null_first_name,
        problems=problems,
    )


@dataclass(frozen=True, slots=True)
class TableCheck:
    """One table's verdict."""

    table: str
    expected: int
    loaded: int
    missing: tuple[str, ...] = ()
    wrong_timestamps: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.missing and not self.wrong_timestamps and self.expected == self.loaded


@dataclass(frozen=True, slots=True)
class ProfileReconciliation:
    """Every profile table, against what was meant to land in it.

    ``empty`` is reported rather than left to be read off a zero. A dev snapshot
    legitimately writes no ``mentee_goal_countries`` — ``Country Goal`` is blank
    on every row — and a reconciliation that stays silent about it invites the
    reader to treat the whole run as covered.
    """

    checks: tuple[TableCheck, ...]
    empty: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks)

    def report(self) -> str:
        lines = []
        for check in self.checks:
            verdict = "ok" if check.ok else "FAIL"
            lines.append(
                f"{verdict:4} {check.table:26} expected {check.expected:4}  "
                f"loaded {check.loaded:4}  missing {len(check.missing):3}  "
                f"stamped-by-importer {len(check.wrong_timestamps):3}"
            )
        if self.empty:
            lines.append(
                f"     wrote no rows to: {', '.join(self.empty)} (verify this is expected)"
            )
        return "\n".join(lines)


#: The four tables carrying their own Bubble id, and therefore their own source
#: timestamps, each with its query written out.
#:
#: Literal rather than interpolated. Four ``SELECT``s spelled in full is more
#: text than one f-string over a tuple of names, and it is the house style for a
#: reason: bandit flags the f-string, the honest answers are a validated
#: allow-list or a suppression, and this needs neither. The three junctions are
#: derived from list columns of a parent Thing (settled decision #27) — no
#: anchor, and no source timestamp to preserve — so a count is all there is.
STAMPED = (
    "SELECT legacy_bubble_id, created_at, updated_at FROM {} WHERE legacy_bubble_id IS NOT NULL"
)
ANCHORED_QUERIES: dict[str, str] = {
    "mentor_profiles": STAMPED.format("mentor_profiles"),
    "mentee_goals": STAMPED.format("mentee_goals"),
    "education_entries": STAMPED.format("education_entries"),
    "user_awards": STAMPED.format("user_awards"),
}

JUNCTION_QUERIES: dict[str, str] = {
    "mentor_service_offerings": "SELECT count(*) FROM mentor_service_offerings",
    "mentee_goal_countries": "SELECT count(*) FROM mentee_goal_countries",
    "mentee_goal_needs": "SELECT count(*) FROM mentee_goal_needs",
}


class Anchored(Protocol):
    """A row that carries its own Bubble id and its own source timestamps.

    Structural rather than a base class: the four row types are frozen
    dataclasses in ``domain/``, and giving them a shared ancestor to satisfy a
    check in ``infra/`` would point the dependency the wrong way.

    Declared as read-only properties, not as attributes. A ``Protocol`` with
    plain attributes demands they be *settable*, which no frozen dataclass is —
    so the attribute form rejects all four of the types it was written to
    describe.
    """

    @property
    def legacy_bubble_id(self) -> str: ...

    @property
    def created_at(self) -> datetime | None: ...

    @property
    def updated_at(self) -> datetime | None: ...


async def reconcile_profiles(
    connection: AsyncConnection, plan: ProfilePlan, counts: ProfileCounts
) -> ProfileReconciliation:
    """Compare each profile table against the rows that were meant to land.

    ``expected`` maps a table to ``{legacy_bubble_id: (created_at, updated_at)}``.
    Timestamps are compared **to the microsecond**, for the reason
    ``reconcile_users`` gives: a load that ran with ``trg_set_updated_at`` enabled
    produces values within a second or two of each other and of ``now()``, so a
    tolerant comparison passes exactly the failure this exists to catch. Every
    row present, every count agreeing, and every timestamp quietly rewritten.
    """

    # Built here rather than by the caller. `scripts/` is a composition root and
    # may hold no business rule, and "which rows were meant to land in which
    # table" is exactly that.
    def stamps(rows: Sequence[Anchored]) -> dict[str, tuple[object, object]]:
        return {row.legacy_bubble_id: (row.created_at, row.updated_at) for row in rows}

    expected: dict[str, dict[str, tuple[object, object]]] = {
        "mentor_profiles": stamps(plan.mentors),
        "mentee_goals": stamps(plan.goals),
        "education_entries": stamps(plan.education),
        "user_awards": stamps(plan.awards),
    }
    junction_counts = {
        "mentor_service_offerings": counts.mentor_services,
        "mentee_goal_countries": counts.goal_countries,
        "mentee_goal_needs": counts.goal_needs,
    }

    checks: list[TableCheck] = []

    for table, query in ANCHORED_QUERIES.items():
        wanted = expected.get(table, {})
        result = await connection.execute(text(query))
        loaded = {row.legacy_bubble_id: row for row in result}
        missing = tuple(sorted(set(wanted) - set(loaded)))
        wrong = tuple(
            sorted(
                key
                for key, (created, updated) in wanted.items()
                if key in loaded
                and (loaded[key].created_at != created or loaded[key].updated_at != updated)
            )
        )
        checks.append(
            TableCheck(
                table=table,
                expected=len(wanted),
                loaded=len(loaded),
                missing=missing,
                wrong_timestamps=wrong,
            )
        )

    for table, query in JUNCTION_QUERIES.items():
        result = await connection.execute(text(query))
        checks.append(
            TableCheck(table=table, expected=junction_counts[table], loaded=result.scalar_one())
        )

    return ProfileReconciliation(
        checks=tuple(checks),
        empty=tuple(check.table for check in checks if check.loaded == 0),
    )
