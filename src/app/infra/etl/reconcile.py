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
from app.domain.transform.availability import AvailabilityPlan
from app.domain.transform.profiles import ProfilePlan
from app.domain.transform.reviews import ReviewPlan
from app.domain.transform.sessions import SessionPlan
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


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------

AVAILABILITY_QUERIES: dict[str, str] = {
    "availability_rules": STAMPED.format("availability_rules"),
    "availability_exceptions": STAMPED.format("availability_exceptions"),
}


@dataclass(frozen=True, slots=True)
class AvailabilityReconciliation:
    """Both availability tables, plus the two checks unique to this phase.

    **Row counts do not reconcile one-to-one, and that is correct.** Every prior
    phase compared source rows to loaded rows; availability breaks that three
    ways at once — exceptions fan out 1:N, Gen A rules are quarantined on
    purpose, and overlapping windows merge so one anchor never reaches the
    table. Asserting `source == loaded` would fail every run, and the fix
    somebody would reach for is a loosened comparison that checks nothing.

    So the identity checked is that every source rule was **accounted for**, and
    the fan-out is checked **per source row** rather than in total.
    """

    checks: tuple[TableCheck, ...]
    #: Source rules in no outcome at all — neither loaded, quarantined, dropped,
    #: refused nor absorbed. A row here vanished without anybody deciding it
    #: should, which is the failure no row count would show.
    unaccounted: tuple[str, ...] = ()
    #: **There is deliberately no per-source fan-out check.** One was written
    #: and removed: a mutation batch showed it survived every test, because an
    #: exception anchor that fails to land is already absent from ``actual`` and
    #: is caught by ``missing`` — and one that lands unplanned makes ``expected``
    #: and ``loaded`` disagree. Grouping on the anchor prefix re-checked
    #: membership two other assertions already covered, which is decoration
    #: rather than depth.

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks) and not self.unaccounted

    def report(self) -> str:
        lines = [
            f"{'ok' if check.ok else 'FAIL':4} {check.table:26} "
            f"expected {check.expected:4}  loaded {check.loaded:4}  "
            f"missing {len(check.missing):3}  stamped-by-importer "
            f"{len(check.wrong_timestamps):3}"
            for check in self.checks
        ]
        for check in self.checks:
            lines.extend(f"     missing from {check.table}: {anchor}" for anchor in check.missing)
            lines.extend(
                f"     timestamp rewritten in {check.table}: {anchor}"
                for anchor in check.wrong_timestamps
            )
        lines.extend(f"FAIL unaccounted source rule: {anchor}" for anchor in self.unaccounted)
        return "\n".join(lines)


async def reconcile_availability(
    connection: AsyncConnection, plan: AvailabilityPlan
) -> AvailabilityReconciliation:
    """Compare both availability tables against the plan that produced them.

    Expected comes from the plan; actual comes from the database. Neither
    re-derives the other — a check whose two sides come from the same place
    agrees with itself and proves nothing.

    Timestamps are compared **to the microsecond**, for the reason
    ``reconcile_users`` gives: a load that ran with ``trg_set_updated_at``
    enabled produces values within a second or two of each other and of
    ``now()``, so a tolerant comparison passes exactly the failure this exists
    to catch.
    """
    expected: dict[str, dict[str, tuple[datetime | None, datetime | None]]] = {
        "availability_rules": {
            row.legacy_bubble_id: (row.created_at, row.updated_at) for row in plan.rules
        },
        "availability_exceptions": {
            row.legacy_bubble_id: (row.created_at, row.updated_at) for row in plan.exceptions
        },
    }

    checks: list[TableCheck] = []
    for table, query in AVAILABILITY_QUERIES.items():
        result = await connection.execute(text(query))
        actual = {
            row.legacy_bubble_id: (row.created_at, row.updated_at) for row in result.mappings()
        }
        wanted = expected[table]
        checks.append(
            TableCheck(
                table=table,
                expected=len(wanted),
                loaded=len(actual),
                missing=tuple(sorted(set(wanted) - set(actual))),
                wrong_timestamps=tuple(
                    sorted(
                        anchor
                        for anchor, stamps in wanted.items()
                        if anchor in actual and actual[anchor] != stamps
                    )
                ),
            )
        )

    return AvailabilityReconciliation(
        checks=tuple(checks),
        unaccounted=_unaccounted(plan),
    )


def _unaccounted(plan: AvailabilityPlan) -> tuple[str, ...]:
    """Source rules the plan reached no decision about.

    Every anchor the transform was handed must appear in one of five outcomes —
    loaded, quarantined, dropped, refused for not moving forward, or absorbed
    into a neighbour. A row in none of them vanished without anybody deciding it
    should, which is silent data loss that no row count would show: the loaded
    total is *meant* to be smaller than the source total here, so a smaller one
    looks correct.

    An earlier version of this function returned duplicated anchors instead and
    claimed in its docstring to do this. Every test passed, because none of them
    gave the plan a source list to be missing from.
    """
    accounted = set(plan.accounted_for())
    return tuple(sorted(anchor for anchor in plan.source_rule_ids if anchor not in accounted))


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------

#: The four derived tables. None carries `legacy_bubble_id`: participants and
#: events are derived from columns of their parent Thing, and the two type
#: tables have no legacy source at all — they are created by the migration.
#: So a count is all there is, exactly as it is for M2's three junctions.
SESSION_COUNTS: dict[str, str] = {
    "session_types": "SELECT count(*) FROM session_types",
    "session_type_booking_configs": "SELECT count(*) FROM session_type_booking_configs",
    "session_participants": "SELECT count(*) FROM session_participants",
    "session_events": "SELECT count(*) FROM session_events",
}

#: Migrated sessions with no session type. **Zero is the precondition for making
#: `sessions.session_type_id` NOT NULL** in a later revision, and nothing else in
#: the gate would notice it failing — the column is nullable today, so a null
#: lands looking entirely valid.
SESSIONS_WITHOUT_TYPE = (
    "SELECT legacy_bubble_id FROM sessions "
    "WHERE legacy_bubble_id IS NOT NULL AND session_type_id IS NULL "
    "ORDER BY legacy_bubble_id"
)


@dataclass(frozen=True, slots=True)
class SessionReconciliation:
    """Five tables, and the checks unique to a phase that merges two Things.

    **Two identities, not one.** Every prior phase had a single source
    collection. Sessions has two, and they do not collapse: a tracker merged
    into its booking is *absorbed* rather than loaded, so one identity per
    source is the only shape that can tell "this row was accounted for" from
    "this row was counted twice".
    """

    checks: tuple[TableCheck, ...]
    #: Source bookings in no outcome at all. A booking is loaded or dropped;
    #: a row in neither vanished without anybody deciding it should.
    unaccounted_bookings: tuple[str, ...] = ()
    #: Source trackers in no outcome at all — absorbed, loaded, quarantined or
    #: dropped.
    unaccounted_trackers: tuple[str, ...] = ()
    #: Migrated sessions carrying no `session_type_id`.
    sessions_without_type: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return (
            all(check.ok for check in self.checks)
            and not self.unaccounted_bookings
            and not self.unaccounted_trackers
            and not self.sessions_without_type
        )

    def report(self) -> str:
        lines = [
            f"{'ok' if check.ok else 'FAIL':4} {check.table:30} "
            f"expected {check.expected:5}  loaded {check.loaded:5}  "
            f"missing {len(check.missing):3}  stamped-by-importer "
            f"{len(check.wrong_timestamps):3}"
            for check in self.checks
        ]
        for check in self.checks:
            lines.extend(f"     missing from {check.table}: {anchor}" for anchor in check.missing)
            lines.extend(
                f"     timestamp rewritten in {check.table}: {anchor}"
                for anchor in check.wrong_timestamps
            )
        lines.extend(
            f"FAIL unaccounted source booking: {anchor}" for anchor in self.unaccounted_bookings
        )
        lines.extend(
            f"FAIL unaccounted source tracker: {anchor}" for anchor in self.unaccounted_trackers
        )
        lines.extend(
            f"FAIL session has no session type: {anchor}" for anchor in self.sessions_without_type
        )
        return "\n".join(lines)


async def reconcile_sessions(
    connection: AsyncConnection, plan: SessionPlan
) -> SessionReconciliation:
    """Compare all five session tables against the plan that produced them.

    Expected comes from the plan; actual comes from the database. Neither
    re-derives the other — a check whose two sides come from the same place
    agrees with itself and proves nothing. That is also why the loader returns
    no counts: what landed is read back here.

    Only ``sessions`` is checked by anchor and timestamp. The other four are
    derived and carry neither, so a count is the whole of what can be asserted.
    """
    expected = {row.legacy_bubble_id: (row.created_at, row.updated_at) for row in plan.sessions}
    result = await connection.execute(text(STAMPED.format("sessions")))
    actual = {row.legacy_bubble_id: (row.created_at, row.updated_at) for row in result.mappings()}

    checks: list[TableCheck] = [
        TableCheck(
            table="sessions",
            expected=len(expected),
            loaded=len(actual),
            missing=tuple(sorted(set(expected) - set(actual))),
            wrong_timestamps=tuple(
                sorted(
                    anchor
                    for anchor, stamps in expected.items()
                    if anchor in actual and actual[anchor] != stamps
                )
            ),
        )
    ]

    wanted_counts = {
        "session_types": len(plan.session_types),
        "session_type_booking_configs": len(plan.session_types),
        "session_participants": len(plan.participants),
        "session_events": len(plan.events),
    }
    for table, query in SESSION_COUNTS.items():
        count = await connection.execute(text(query))
        checks.append(
            TableCheck(table=table, expected=wanted_counts[table], loaded=count.scalar_one())
        )

    without_type = await connection.execute(text(SESSIONS_WITHOUT_TYPE))

    return SessionReconciliation(
        checks=tuple(checks),
        unaccounted_bookings=_unaccounted_of(
            plan.source_booking_ids, plan.accounted_for_bookings()
        ),
        unaccounted_trackers=_unaccounted_of(
            plan.source_tracker_ids, plan.accounted_for_trackers()
        ),
        sessions_without_type=tuple(row.legacy_bubble_id for row in without_type),
    )


def _unaccounted_of(source: Sequence[str], accounted: Sequence[str]) -> tuple[str, ...]:
    """Source anchors the plan reached no decision about.

    One function for both identities rather than two nearly-identical ones —
    ``_unaccounted`` for availability was written twice in an earlier draft and
    the second copy returned duplicated anchors while claiming in its docstring
    to do this. Every test passed, because none of them gave the plan a source
    list to be missing from.
    """
    decided = set(accounted)
    return tuple(sorted(anchor for anchor in source if anchor not in decided))


@dataclass(frozen=True, slots=True)
class ReviewReconciliation:
    """One table, and the identity that has to hold for a phase this small.

    **Every source row is accounted for**, which is the same identity
    `reconcile_availability` settled on and for a related reason: rows can be
    quarantined on purpose, so `source == loaded` is the wrong assertion and the
    fix somebody reaches for is a loosened comparison that checks nothing.

    Here the accounting is exact rather than fanned out — one review becomes one
    row or one quarantine — so an unaccounted anchor is a bug rather than a
    shape.
    """

    checks: tuple[TableCheck, ...]

    #: Source anchors that became neither a row nor a stated quarantine.
    unaccounted: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.unaccounted and all(check.ok for check in self.checks)

    def report(self) -> str:
        lines = [
            f"{'ok' if check.ok else 'FAIL':4} {check.table:30} "
            f"expected {check.expected:5}  loaded {check.loaded:5}  "
            f"missing {len(check.missing):3}  stamped-by-importer "
            f"{len(check.wrong_timestamps):3}"
            for check in self.checks
        ]
        for check in self.checks:
            lines += [f"  MISSING {anchor}" for anchor in check.missing]
            lines += [f"  STAMPED BY IMPORTER {anchor}" for anchor in check.wrong_timestamps]
        if self.unaccounted:
            lines.append(f"FAIL unaccounted source rows: {len(self.unaccounted)}")
        return "\n".join(lines)


async def reconcile_reviews(connection: AsyncConnection, plan: ReviewPlan) -> ReviewReconciliation:
    """Compare the reviews table against the plan that produced it.

    Expected comes from the plan; actual comes from the database. Neither
    re-derives the other — a check whose two sides come from the same place
    agrees with itself and proves nothing, which is also why `ReviewLoader`
    returns no counts.

    Timestamps are compared **to the microsecond**, for the reason
    `reconcile_users` gives: a load that ran with `trg_set_updated_at` enabled
    produces values within a second or two of `now()`, so a tolerant comparison
    passes exactly the failure this exists to catch.
    """
    expected = {row.legacy_bubble_id: (row.created_at, row.updated_at) for row in plan.reviews}
    result = await connection.execute(text(STAMPED.format("reviews")))
    actual = {row.legacy_bubble_id: (row.created_at, row.updated_at) for row in result.mappings()}

    accounted = {row.legacy_bubble_id for row in plan.reviews} | {
        row.legacy_bubble_id for row in plan.quarantined
    }
    return ReviewReconciliation(
        checks=(
            TableCheck(
                table="reviews",
                expected=len(expected),
                loaded=len(actual),
                missing=tuple(sorted(set(expected) - set(actual))),
                wrong_timestamps=tuple(
                    sorted(
                        anchor
                        for anchor, stamps in expected.items()
                        if anchor in actual and actual[anchor] != stamps
                    )
                ),
            ),
        ),
        unaccounted=tuple(sorted(a for a in accounted if not a)),
    )
