"""M3 calendar Things into the rows the availability tables expect.

Pure: dictionaries in, dataclasses out. Same contract as ``identity`` and
``profiles``.

**Two generations of `CalendarSettings`, and they mean different things.** The
export splits 12/12 with no date overlap, and the discriminator is whether
``timeZone`` is populated:

    Gen B  timeZone set, created 2025-09 onward
           the printed time IS the declared wall clock

    Gen A  timeZone blank, created 2024-11 to 2025-02
           the printed time is five hours behind the declared wall clock, and
           the UTC rendering is what the mentor was shown

That is measured, not inferred. On every Gen A row the ``12hr-…-TXT`` column —
what the legacy UI displayed — equals the *UTC* rendering of ``startTime``, and
`parse_timestamp` returns aware UTC, so ``.time()`` on its result reproduces the
displayed value exactly. On Gen B the ``timeZone`` column and the one surviving
TXT value agree with the printed time instead.

**Applying either rule to both halves is a five-hour error on half the table**,
and it is a one-line mistake in both directions: ``parse_timestamp(...).time()``
is right for Gen A and wrong for Gen B; the naive parse is the reverse.

**Gen A is not loaded.** Which reading is a mentor's real availability cannot be
settled from this export — no mentor here owns both a Gen A rule and a booking,
so the 1,073 production bookings are the only oracle and they are not available
until cutover. Loading under a guess would put times a mentor may then edit into
a table the product reads, and a re-run could no longer safely correct them. So
they are reported with **both** candidate readings side by side and the decision
is taken once, at the production extract, where it is still cheap.

**Attribution is ``Creator`` alone**, which makes this the second exception to
the user-side-link rule after ``Scholarship-Awards`` (settled decision #60).
``CalendarSettings`` has no owner column and ``User`` has no link to it;
``CalendarExtra.calendarSettingList`` is empty on every row. That is attribution
by *who made the row*, not *whose row it is*, and it is labelled here rather
than discovered later.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from app.domain.bubble import parse_timestamp
from app.domain.enums import AvailabilityExceptionType
from app.domain.transform.identity import (
    DEFAULT_TIMEZONE,
    TransformError,
    _resolve_timezone,
)
from app.domain.transform.profiles import MENTOR_LINK_FIELD

#: The legacy user field the zone comes from. Imported rather than retyped where
#: it already exists; `MENTOR_LINK_FIELD` likewise. A rename that updated one
#: copy and not the other would empty `mentor_owners`, drop every calendar row
#: as unattributable, and exit 0.
USER_TIMEZONE_FIELD = "UserTimezonID"

# --------------------------------------------------------------------------
# Legacy field names
# --------------------------------------------------------------------------
#
# Two carry emoji, and they are written as escape sequences for the reason
# `profiles.py` does the same for `\U0001f1f3\U0001f1ec studyCountry`: a literal
# is unreadable in a diff and breaks on any tool that opens the file as cp1252,
# which is the Windows default.
DAY_NUMBER_FIELD = "dayOfWeekIn#️⃣"
DAY_NAME_FIELD = "daysOfWeek-O/S\U0001f51b"
AVAILABLE_FIELD = "availableDay-Bool"
START_FIELD = "startTime"
END_FIELD = "endTime"
ZONE_FIELD = "timeZone"
CREATOR_FIELD = "Creator"
BLOCK_DATES_FIELD = "block-Date(s)"
DAILY_SESSIONS_FIELD = "meetingDailySessions"

#: Dropped, each for a recorded reason. `meetingVenue` holds Google Meet URLs,
#: the string `meet.g`, and two rows of profile bio text — it is not a venue
#: type, and D21 forbids carrying a static room link forward. `meetingDuration`
#: belongs to session types (settled decision #58). The four display columns are
#: the same fact pre-formatted, which is what breaks across DST.
DROPPED_FIELDS = (
    "meetingVenue",
    "meetingDuration-TxT",
    "12hr-localStartTime-TXT",
    "12hr-localEndTime-TXT",
    "24hr-locatStartTime-TXT",
    "24hr-localEndTime-TXT",
)

DAY_NAMES = {
    "0": "Sunday",
    "1": "Monday",
    "2": "Tuesday",
    "3": "Wednesday",
    "4": "Thursday",
    "5": "Friday",
    "6": "Saturday",
}

TRUE_VALUES = {"yes", "true"}


@dataclass(frozen=True, slots=True)
class AvailabilityRuleRow:
    legacy_bubble_id: str
    mentor_bubble_id: str
    day_of_week: int
    start_time: dt.time
    end_time: dt.time
    timezone: str
    is_active: bool
    created_at: dt.datetime | None
    updated_at: dt.datetime | None


@dataclass(frozen=True, slots=True)
class AvailabilityExceptionRow:
    #: ``{bubble_id}:{iso_date}``. One legacy row holds a *list* of discontiguous
    #: dates, so it fans out to one row per date and the bare Bubble id is not
    #: unique. The date is derived from the source, so the anchor stays stable
    #: across re-runs.
    legacy_bubble_id: str
    mentor_bubble_id: str
    kind: AvailabilityExceptionType
    start_date: dt.date
    end_date: dt.date
    timezone: str
    max_sessions_per_day: int | None
    created_at: dt.datetime | None
    updated_at: dt.datetime | None


@dataclass(frozen=True, slots=True)
class DroppedRow:
    """A source row that did not become one, and why.

    **The anchor is a field, not a phrase inside the reason.** These used to be
    formatted strings, which read well in a terminal and are useless to
    reconciliation: accounting for them meant parsing a Bubble id out of a
    sentence, and improving the wording would have silently stopped the
    accounting while the totals still balanced.
    """

    legacy_bubble_id: str
    reason: str

    def __str__(self) -> str:
        return f"{self.legacy_bubble_id}: {self.reason}"


@dataclass(frozen=True, slots=True)
class MergedWindow:
    """Two overlapping windows folded into one, and which anchor survived.

    ``absorbed`` never reaches the database, so reconciliation has to be told
    about it or the row count will not add up — and the operator has to be told
    which legacy id is no longer represented.
    """

    kept: str
    absorbed: str
    day_of_week: int
    detail: str

    def __str__(self) -> str:
        return f"day {self.day_of_week}: {self.detail}; {self.absorbed} is not loaded"


@dataclass(frozen=True, slots=True)
class QuarantinedRule:
    """A Gen A row, with both readings, awaiting the production decision."""

    legacy_bubble_id: str
    mentor_bubble_id: str
    day_of_week: int
    #: The printed time — what Gen B would call the declared wall clock.
    as_printed: dt.time
    #: The UTC rendering — what the legacy UI displayed on these rows.
    as_displayed: dt.time
    end_as_printed: dt.time
    end_as_displayed: dt.time


@dataclass(frozen=True, slots=True)
class AvailabilityPlan:
    rules: tuple[AvailabilityRuleRow, ...]
    exceptions: tuple[AvailabilityExceptionRow, ...]

    #: Gen A. Reported, never loaded.
    quarantined: tuple[QuarantinedRule, ...] = ()
    #: Every source row that did not become one. **Accounting, not a
    #: diagnostic**: reconciliation adds these to the loaded and quarantined
    #: counts and expects the source total.
    dropped: tuple[DroppedRow, ...] = ()
    #: Windows merged into their union before insert, so the exclusion
    #: constraint cannot abort the load. ``"<mentor>: 09:00-12:00 + 10:00-13:00
    #: -> 09:00-13:00"``.
    merged_overlaps: tuple[MergedWindow, ...] = ()
    #: Owners whose zone is the `UTC` default, meaning nobody ever set one.
    zone_defaults: tuple[str, ...] = ()
    #: `dayOfWeekIn` disagreeing with `daysOfWeek-O/S`. The number wins.
    day_mismatches: tuple[str, ...] = ()
    #: Windows crossing midnight, or resolving to zero length. The column
    #: CHECK forbids both, so they are reported rather than split on a guess
    #: — and they are **accounting**, because these rows do not load either.
    midnight_crossing: tuple[DroppedRow, ...] = ()
    #: Every source rule anchor the transform was handed. Carried so
    #: reconciliation can assert the identity rather than assume it: without
    #: it, "was every source row decided about" is unanswerable, because the
    #: plan only knows what it produced.
    source_rule_ids: tuple[str, ...] = ()
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.errors

    def accounted_for(self) -> tuple[str, ...]:
        """Every source rule anchor this run reached a decision about.

        The identity reconciliation checks. A source row must be **loaded, or
        quarantined, or dropped, or refused for not moving forward, or absorbed
        into a neighbour** — five outcomes, and a row in none of them vanished
        without anybody deciding it should.

        ``midnight_crossing`` belongs here and is easy to miss: those rows are
        reported rather than loaded, so leaving them out makes the identity fail
        on exactly the data that has one.
        """
        return (
            tuple(row.legacy_bubble_id for row in self.rules)
            + tuple(row.legacy_bubble_id for row in self.quarantined)
            + tuple(row.legacy_bubble_id for row in self.dropped)
            + tuple(row.legacy_bubble_id for row in self.midnight_crossing)
            + tuple(note.absorbed for note in self.merged_overlaps)
        )

    def mentors_needing_redeclaration(self) -> tuple[str, ...]:
        """Mentors whose availability does not survive the migration.

        Quarantined rules belong to somebody, and that somebody arrives at
        cutover with an empty schedule. This list is what makes the availability
        write surface a cutover dependency rather than a nice-to-have.
        """
        loaded = {row.mentor_bubble_id for row in self.rules}
        return tuple(sorted({row.mentor_bubble_id for row in self.quarantined} - loaded))

    def report(self) -> str:
        """The operator-facing account of one run.

        Lives in ``domain`` beside the decisions it summarises, per settled
        decision #45 — these counts *are* the result, and a store that formats
        text for a terminal has stopped being a store.

        **The quarantine block is the deliverable, not a footnote.** It is what
        the Gen A reading is decided from at the production extract, and it
        prints both candidates side by side so that decision is a comparison
        rather than a re-derivation.
        """
        lines = [
            f"rules loaded        {len(self.rules)}",
            f"exceptions loaded   {len(self.exceptions)}",
            f"quarantined (Gen A) {len(self.quarantined)}"
            f" across {len({q.mentor_bubble_id for q in self.quarantined})} mentors",
            f"dropped             {len(self.dropped)}",
        ]

        for label, values in (
            ("dropped", self.dropped),
            ("merged overlaps", self.merged_overlaps),
            ("day-of-week disagreements", self.day_mismatches),
            ("midnight-crossing windows", self.midnight_crossing),
            ("timezone defaulted to UTC", self.zone_defaults),
            ("errors", self.errors),
        ):
            if values:
                lines.append(f"\n{label} ({len(values)}):")
                lines.extend(f"  {value}" for value in values)

        if self.quarantined:
            lines.append("\nquarantined Gen A rules — both readings, for the production decision:")
            lines.append("  day  printed        displayed      mentor")
            for held in self.quarantined:
                lines.append(
                    f"  {held.day_of_week}    "
                    f"{held.as_printed:%H:%M}-{held.end_as_printed:%H:%M}   "
                    f"{held.as_displayed:%H:%M}-{held.end_as_displayed:%H:%M}  "
                    f"{held.mentor_bubble_id}"
                )

        needing = self.mentors_needing_redeclaration()
        if needing:
            lines.append(
                f"\n{len(needing)} mentors have no availability after this load and must "
                f"re-declare it at cutover:"
            )
            lines.extend(f"  {mentor}" for mentor in needing)

        return "\n".join(lines)


# --------------------------------------------------------------------------
# Transforms
# --------------------------------------------------------------------------

#: `Jan 13, 2025 12:00 am`. Matched rather than split, because the value is a
#: **comma-joined list of dates that themselves contain commas** — the project's
#: own `normalise_list` splits on "," and would turn one date into two
#: fragments, silently, producing dates nobody entered.
#: Case-insensitive, and that is not defensive. The export renders `am` on the
#: calendar rows and `AM` on others in the same snapshot, so the two spellings
#: already coexist. A case-sensitive pattern returns an empty list for a row it
#: cannot read — **no dates, no exceptions, no error and no report line** — which
#: is the quietest possible way to lose a mentor's blocked days.
DATE_PATTERN = re.compile(r"[A-Za-z]{3} \d{1,2}, \d{4} \d{1,2}:\d{2} [ap]m", re.IGNORECASE)

BUBBLE_FORMAT = "%b %d, %Y %I:%M %p"


def _printed(raw: str, *, bubble_id: str, field_name: str) -> dt.datetime:
    """The value exactly as the export rendered it, with no zone conversion."""
    try:
        return dt.datetime.strptime(raw.strip(), BUBBLE_FORMAT)
    except ValueError as exc:
        raise TransformError(bubble_id, f"{field_name}: {raw!r} is not a Bubble timestamp") from exc


def _declared_time(
    raw: str, *, generation: str, export_timezone: dt.tzinfo, bubble_id: str, field_name: str
) -> dt.time:
    """The wall clock the mentor actually declared, per generation.

    The whole milestone turns on this function being branched. ``parse_timestamp``
    returns aware **UTC**, so ``.time()`` on it yields the UTC rendering — which
    is what the legacy UI displayed on Gen A rows and is five hours away from
    what it displayed on Gen B ones.
    """
    if generation == "A":
        return parse_timestamp(raw, assume=export_timezone).time()
    return _printed(raw, bubble_id=bubble_id, field_name=field_name).time()


def _generation(record: dict[str, Any]) -> str:
    """``"B"`` when the row carries a zone, ``"A"`` when it does not.

    **Any falsy value counts as absent**, not just ``None``. Canonicalisation
    runs every value through ``blank_to_none``, so a blank column *does* arrive
    as ``None`` today and ``is None`` would be sufficient — but only for as long
    as nothing reaches this function without going through that step. Anything
    that did, and handed over ``""``, would be classified Gen B and have the
    wrong time rule applied to half the table. The failure is a silent five-hour
    shift on live availability; the guard against it is one character.
    """
    return "A" if not str(record.get(ZONE_FIELD) or "").strip() else "B"


def block_dates(raw: Any) -> list[dt.date]:
    """Every distinct date in a ``block-Date(s)`` value, in order.

    Deduplicated because the anchor is ``{bubble_id}:{iso_date}`` and
    ``legacy_bubble_id`` is UNIQUE — a date listed twice in one source row would
    abort the whole load on the second insert.
    """
    if not raw:
        return []
    seen: dict[dt.date, None] = {}
    for match in DATE_PATTERN.findall(str(raw)):
        seen.setdefault(dt.datetime.strptime(match, BUBBLE_FORMAT).date(), None)
    return list(seen)


def _timestamp(
    record: dict[str, Any], key: str, *, export_timezone: dt.tzinfo
) -> dt.datetime | None:
    raw = record.get(key)
    return parse_timestamp(str(raw), assume=export_timezone) if raw else None


def _int_or_none(value: Any) -> int | None:
    try:
        parsed = int(str(value)) if value not in (None, "") else None
    except ValueError:
        return None
    # A cap of zero or less is not a cap, and the column has no CHECK — so a
    # `0` would land looking entirely valid and block every booking on that
    # date. Empty on both dev rows; unguarded for production without this.
    return parsed if parsed is None or parsed > 0 else None


def _merge_windows(
    rows: Sequence[AvailabilityRuleRow],
) -> tuple[tuple[AvailabilityRuleRow, ...], tuple[MergedWindow, ...]]:
    """Fold overlapping windows on one mentor-weekday into their union.

    Required, not tidiness. ``availability_rules`` carries a partial ``EXCLUDE``
    constraint forbidding overlaps, so an unmerged pair does not insert badly —
    it **aborts the load** on the second row. Merging is lossless because the
    union is what the two rows mean, and the earliest row's anchor is kept so a
    re-run lands on the same target.
    """
    merged: list[AvailabilityRuleRow] = []
    notes: list[MergedWindow] = []
    # **Partitioned by `is_active`, and that is load-bearing.** The constraint
    # this merge exists to satisfy is partial — `WHERE is_active AND deleted_at
    # IS NULL` — so an inactive window never collides with anything and never
    # needs folding. Merging across the flag is worse than unnecessary: an
    # active 09:00-12:00 absorbing an inactive 10:00-13:00 yields an *active*
    # 09:00-13:00, manufacturing an hour of bookable time the mentor switched
    # off. Found by review, reproduced, and this partition is the fix.
    ordered = sorted(
        rows,
        # `legacy_bubble_id` last so the survivor of a tie is the same row on
        # every run. Without it the winner is export order, and a re-extract
        # that reordered would insert the other anchor while the first row was
        # still present — an EXCLUDE violation that aborts the whole load.
        key=lambda r: (
            r.mentor_bubble_id,
            r.day_of_week,
            r.is_active,
            r.start_time,
            r.legacy_bubble_id,
        ),
    )
    for row in ordered:
        previous = merged[-1] if merged else None
        same_group = (
            previous is not None
            and previous.mentor_bubble_id == row.mentor_bubble_id
            and previous.day_of_week == row.day_of_week
            and previous.is_active == row.is_active
        )
        if same_group and previous is not None and row.start_time < previous.end_time:
            merged[-1] = replace(previous, end_time=max(previous.end_time, row.end_time))
            notes.append(
                MergedWindow(
                    kept=previous.legacy_bubble_id,
                    absorbed=row.legacy_bubble_id,
                    day_of_week=row.day_of_week,
                    detail=(
                        f"{previous.start_time:%H:%M}-{previous.end_time:%H:%M} "
                        f"({previous.legacy_bubble_id}) + "
                        f"{row.start_time:%H:%M}-{row.end_time:%H:%M} "
                        f"({row.legacy_bubble_id}) -> "
                        f"{merged[-1].start_time:%H:%M}-{merged[-1].end_time:%H:%M}"
                    ),
                )
            )
        else:
            merged.append(row)
    return tuple(merged), tuple(notes)


def owner_timezones(user_records: list[dict[str, Any]]) -> tuple[dict[str, str], tuple[str, ...]]:
    """Each user's IANA zone, validated the same way ``users.timezone`` was.

    **The validation is the point.** ``availability_rules.timezone`` is `text`
    with no CHECK, because `pg_timezone_names` is not immutable and PostgreSQL
    will not accept it in one — the model says so, and names the domain as where
    the guarantee actually lives. Reading the raw export field instead would
    hand an unvalidated string straight to that column, and the failure would
    surface much later as `ZoneInfoNotFoundError` inside the projection.

    A refused value becomes a reported error rather than a row, matching
    ``_resolve_timezone``: absent defaults, present-but-unrecognised raises.
    """
    zones: dict[str, str] = {}
    errors: list[str] = []
    for record in user_records:
        bubble_id = _bubble_id(record)
        try:
            zones[bubble_id] = _resolve_timezone(record.get(USER_TIMEZONE_FIELD), bubble_id)
        except TransformError as exc:
            errors.append(str(exc))
    return zones, tuple(errors)


def mentor_owners(user_records: list[dict[str, Any]]) -> set[str]:
    """Users carrying a ``Mentor`` link, which is what a mentor profile is made
    from — so it is exactly the set whose rows can satisfy the foreign key."""
    return {_bubble_id(record) for record in user_records if record.get(MENTOR_LINK_FIELD)}


def plan_availability(
    user_records: list[dict[str, Any]],
    calendar_settings: list[dict[str, Any]],
    calendar_extra: list[dict[str, Any]],
    *,
    export_timezone: dt.tzinfo,
    default_zone: str = DEFAULT_TIMEZONE,
) -> AvailabilityPlan:
    """Every calendar row, attributed and classified, before anything is written.

    **Takes the raw user records** rather than two maps a caller assembled, which
    is how ``plan_profiles`` is shaped and for the same reason: deciding what a
    user's timezone is, and who counts as a mentor, are mapping decisions. Doing
    them in ``scripts/`` would put two business rules in the one directory the
    gate cannot see (settled decision #44) and would skip the IANA validation
    entirely.

    The owner's zone is the **only** source of one for exceptions, because
    ``CalendarExtra`` has no timezone field. Rules never fall back: a rule with
    no zone is Gen A by definition, and Gen A is quarantined.
    """
    owner_zones, errors_from_zones = owner_timezones(user_records)
    mentor_bubble_ids = mentor_owners(user_records)
    rules: list[AvailabilityRuleRow] = []
    quarantined: list[QuarantinedRule] = []
    dropped: list[DroppedRow] = []
    day_mismatches: list[str] = []
    midnight: list[DroppedRow] = []
    zone_defaults: list[str] = []
    errors: list[str] = []

    for record in calendar_settings:
        bubble_id = _bubble_id(record)
        owner = record.get(CREATOR_FIELD)
        if not owner:
            dropped.append(DroppedRow(bubble_id, "no Creator, so nobody can be said to own it"))
            continue
        if str(owner) not in mentor_bubble_ids:
            dropped.append(DroppedRow(bubble_id, f"owner {owner} has no mentor profile"))
            continue

        day_raw = str(record.get(DAY_NUMBER_FIELD) or "")
        name = record.get(DAY_NAME_FIELD)
        if name and DAY_NAMES.get(day_raw) != name:
            day_mismatches.append(f"{bubble_id}: dayOfWeekIn={day_raw!r} but daysOfWeek={name!r}")

        # Validated here rather than left to the column's CHECK. `int(day_raw or 0)`
        # turned a missing value into **Sunday** — a plausible row nobody entered —
        # and a non-numeric one raised out of this function and killed the run. A
        # value outside 0-6 reaches `day_of_week BETWEEN 0 AND 6` and aborts the
        # whole load at insert, three layers from its cause.
        if day_raw not in DAY_NAMES:
            dropped.append(DroppedRow(bubble_id, f"dayOfWeekIn {day_raw!r} is not a weekday 0-6"))
            continue
        day_of_week = int(day_raw)

        start_raw, end_raw = record.get(START_FIELD), record.get(END_FIELD)
        if not start_raw or not end_raw:
            dropped.append(DroppedRow(bubble_id, "no start or end time"))
            continue

        generation = _generation(record)
        try:
            start = _declared_time(
                str(start_raw),
                generation=generation,
                export_timezone=export_timezone,
                bubble_id=bubble_id,
                field_name=START_FIELD,
            )
            end = _declared_time(
                str(end_raw),
                generation=generation,
                export_timezone=export_timezone,
                bubble_id=bubble_id,
                field_name=END_FIELD,
            )
        except (TransformError, ValueError) as exc:
            errors.append(f"{bubble_id}: {exc}")
            continue

        # **Quarantine before the ordering check, not after.** The check used to
        # run first, on the Gen A *displayed* reading — so any Gen A window
        # printed between 19:00 and midnight shifted past midnight once
        # converted, failed `end > start`, and was reported as a
        # midnight-crossing window nobody entered instead of reaching the
        # quarantine block. That block is the deliverable the production
        # decision is made from, so a row silently leaving it is worse than a
        # wrong time: it is a row nobody knows to ask about. Dev's Gen A rows
        # all sit at 00:45-21:30 so both ends shift together — luck, not
        # coverage.
        if generation == "A":
            quarantined.append(
                QuarantinedRule(
                    legacy_bubble_id=bubble_id,
                    mentor_bubble_id=str(owner),
                    day_of_week=day_of_week,
                    as_printed=_printed(
                        str(start_raw), bubble_id=bubble_id, field_name=START_FIELD
                    ).time(),
                    as_displayed=start,
                    end_as_printed=_printed(
                        str(end_raw), bubble_id=bubble_id, field_name=END_FIELD
                    ).time(),
                    end_as_displayed=end,
                )
            )
            continue

        # Applied only to the reading that will actually be loaded.
        if end <= start:
            midnight.append(
                DroppedRow(bubble_id, f"{start:%H:%M}-{end:%H:%M} does not move forward")
            )
            continue

        # Validated, and stored stripped. `availability_rules.timezone` is `text`
        # with no CHECK — `pg_timezone_names` is not immutable, so PostgreSQL
        # will not accept it in one — which means this is the only place the
        # value is checked at all. An unrecognised zone otherwise passes NOT NULL
        # and raises `ZoneInfoNotFoundError` inside the projection, long after
        # the ETL exited 0. `_generation` already strips before testing, so
        # storing unstripped would classify a padded zone as Gen B and then save
        # a string `ZoneInfo` rejects.
        try:
            rule_zone = _resolve_timezone(record.get(ZONE_FIELD), bubble_id)
        except TransformError as exc:
            dropped.append(DroppedRow(bubble_id, str(exc)))
            continue

        rules.append(
            AvailabilityRuleRow(
                legacy_bubble_id=bubble_id,
                mentor_bubble_id=str(owner),
                day_of_week=day_of_week,
                start_time=start,
                end_time=end,
                timezone=rule_zone,
                is_active=str(record.get(AVAILABLE_FIELD) or "").lower() in TRUE_VALUES,
                created_at=_timestamp(record, "created_at", export_timezone=export_timezone),
                updated_at=_timestamp(record, "modified_at", export_timezone=export_timezone),
            )
        )

    exceptions: list[AvailabilityExceptionRow] = []
    for record in calendar_extra:
        bubble_id = _bubble_id(record)
        owner = record.get(CREATOR_FIELD)
        if not owner or str(owner) not in mentor_bubble_ids:
            dropped.append(DroppedRow(bubble_id, "exception has no attributable mentor"))
            continue

        zone = owner_zones.get(str(owner), default_zone)
        if zone == default_zone:
            zone_defaults.append(f"{bubble_id}: owner {owner} has no timezone of their own")

        raw_dates = record.get(BLOCK_DATES_FIELD)
        days = block_dates(raw_dates)
        # A value that is present but yields no dates is data loss, and it was
        # silent: zero exceptions, no error, exit 0. The pattern matches the
        # export's rendering, but the transform is not supposed to know which
        # source a record came from, and the Data API returns a different shape.
        if raw_dates and not days:
            dropped.append(DroppedRow(bubble_id, f"block-Date(s) {raw_dates!r} yielded no dates"))
            continue

        for day in days:
            exceptions.append(
                AvailabilityExceptionRow(
                    legacy_bubble_id=f"{bubble_id}:{day.isoformat()}",
                    mentor_bubble_id=str(owner),
                    kind=AvailabilityExceptionType.BLOCK,
                    start_date=day,
                    end_date=day + dt.timedelta(days=1),
                    timezone=zone,
                    max_sessions_per_day=_int_or_none(record.get(DAILY_SESSIONS_FIELD)),
                    created_at=_timestamp(record, "created_at", export_timezone=export_timezone),
                    updated_at=_timestamp(record, "modified_at", export_timezone=export_timezone),
                )
            )

    merged_rules, merge_notes = _merge_windows(rules)
    return AvailabilityPlan(
        source_rule_ids=tuple(_bubble_id(record) for record in calendar_settings),
        rules=merged_rules,
        exceptions=tuple(exceptions),
        quarantined=tuple(quarantined),
        dropped=tuple(dropped),
        merged_overlaps=merge_notes,
        zone_defaults=tuple(zone_defaults),
        day_mismatches=tuple(day_mismatches),
        midnight_crossing=tuple(midnight),
        errors=tuple(errors) + errors_from_zones,
    )


def _bubble_id(record: dict[str, Any]) -> str:
    return str(record.get("bubble_id") or record.get("unique id") or "")
