"""M3 calendar Things into the rows the availability tables expect.

Pure: dictionaries in, dataclasses out. Same contract as ``identity`` and
``profiles``.

**One rule for every row** (decision 360, 2026-11-21; supersedes #81's two
generations and #359). A slot is its stored ``startTime`` and ``endTime``
**in the mentor's own zone** (``users.timezone``, from ``UserTimezonID``), each
stamp converted on its own date, on the legacy weekday. The legacy app held a
slot as one instant picked in the mentor's zone and kept that local clock
through daylight saving, and the bookings show it: Segun's Wednesday sessions
sat at 15:00 Chicago in summer (20:00 UTC) and in winter (21:00 UTC), Ujunwa's
Friday ones at 18:00 Vienna (16:00 and 17:00 UTC). Converting each stamp on its
own date recovers that local clock, and the new app's model (a wall clock plus
an IANA zone, resolved per date) keeps it through every DST change after. The
export is printed in the exporter's zone (``EXPORT_TIMEZONE``), which
``parse_timestamp`` undoes.

**Neither the row's ``timeZone`` nor its ``…-TXT`` display columns are read.**
The bookings follow the mentor's profile zone, not the slot's; and the text was
written once, in whichever browser zone created the row, and often went stale.
A row whose slot zone differs from its owner's is reported for a person to read.
The old reading (the printed clock taken as the slot zone's) put production
slots up to six hours off.

**A window that does not move forward ends one session after its start**
(owner, 2026-11-21): 144 of 191 production slots are exactly the mentor's
session length and 23 more a multiple of it, so a slot is a session. Zero-length
stamps and overnight slips (9:00 pm to 5:30 am) become one session; only a row
with no length to use, or one whose session would pass midnight, is reported.

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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any
from zoneinfo import ZoneInfo

from app.domain.bubble import legacy_anchor, parse_timestamp
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
#: belongs to session types (settled decision #58). The four display columns
#: were written once in the creating browser's zone and often went stale
#: (decision 360). `timeZone` is kept only to report a disagreement.
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
class AvailabilityPlan:
    rules: tuple[AvailabilityRuleRow, ...]
    exceptions: tuple[AvailabilityExceptionRow, ...]

    #: Anchors whose window did not move forward, ended one session after start.
    fitted_to_session: tuple[str, ...] = ()
    #: Rows whose own `timeZone` differs from the owner's (the owner's is used).
    zone_differs: tuple[str, ...] = ()
    #: Every source row that did not become one. **Accounting, not a
    #: diagnostic**: reconciliation adds these to the loaded
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
        dropped, or refused for not moving forward, or absorbed into a
        neighbour** — four outcomes, and a row in none of them vanished without
        anybody deciding it should.

        ``midnight_crossing`` belongs here and is easy to miss: those rows are
        reported rather than loaded, so leaving them out makes the identity fail
        on exactly the data that has one.
        """
        return (
            tuple(row.legacy_bubble_id for row in self.rules)
            + tuple(row.legacy_bubble_id for row in self.dropped)
            + tuple(row.legacy_bubble_id for row in self.midnight_crossing)
            + tuple(note.absorbed for note in self.merged_overlaps)
        )

    def report(self) -> str:
        """The operator-facing account of one run.

        Lives in ``domain`` beside the decisions it summarises, per settled
        decision #45 — these counts *are* the result, and a store that formats
        text for a terminal has stopped being a store.
        """
        lines = [
            f"rules loaded        {len(self.rules)}",
            f"exceptions loaded   {len(self.exceptions)}",
            f"dropped             {len(self.dropped)}",
        ]

        for label, values in (
            ("dropped", self.dropped),
            ("merged overlaps", self.merged_overlaps),
            ("day-of-week disagreements", self.day_mismatches),
            ("midnight-crossing windows", self.midnight_crossing),
            ("ended one session after start (window did not move forward)", self.fitted_to_session),
            ("slot zone differs from the owner's (owner's used)", self.zone_differs),
            ("timezone defaulted to UTC", self.zone_defaults),
            ("errors", self.errors),
        ):
            if values:
                lines.append(f"\n{label} ({len(values)}):")
                lines.extend(f"  {value}" for value in values)

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


def _in_zone(raw: str, *, zone: str, export_timezone: dt.tzinfo) -> dt.time:
    """A stored stamp as the wall clock in ``zone`` on the stamp's own date:
    the time the mentor picked, with the offset that applied when they did."""
    return parse_timestamp(raw, assume=export_timezone).astimezone(ZoneInfo(zone)).time()


def _one_session(start: dt.time, minutes: int | None) -> dt.time | None:
    """``start`` plus one session, or ``None`` with no length or past midnight."""
    if not minutes:
        return None
    day = dt.date(2000, 1, 2)
    end = dt.datetime.combine(day, start) + dt.timedelta(minutes=minutes)
    return end.time() if end.date() == day else None


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

    Every row of one mentor is in that mentor's zone (decision 360), so the
    wall-clock numbers compared here, and by the constraint, mean one thing.
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
        bubble_id = legacy_anchor(record)
        try:
            zones[bubble_id] = _resolve_timezone(record.get(USER_TIMEZONE_FIELD), bubble_id)
        except TransformError as exc:
            errors.append(str(exc))
    return zones, tuple(errors)


def mentor_owners(user_records: list[dict[str, Any]]) -> set[str]:
    """Users carrying a ``Mentor`` link, which is what a mentor profile is made
    from — so it is exactly the set whose rows can satisfy the foreign key."""
    return {legacy_anchor(record) for record in user_records if record.get(MENTOR_LINK_FIELD)}


def plan_availability(
    user_records: list[dict[str, Any]],
    calendar_settings: list[dict[str, Any]],
    calendar_extra: list[dict[str, Any]],
    *,
    export_timezone: dt.tzinfo,
    session_minutes: Mapping[str, int],
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
    ``CalendarExtra`` has no timezone field, and for every rule (decision 360).
    ``session_minutes`` is each mentor's session length
    (``transform.sessions.durations_by_mentor``), passed in because that module
    imports this one.
    """
    owner_zones, errors_from_zones = owner_timezones(user_records)
    mentor_bubble_ids = mentor_owners(user_records)
    rules: list[AvailabilityRuleRow] = []
    fitted_to_session: list[str] = []
    zone_differs: list[str] = []
    dropped: list[DroppedRow] = []
    day_mismatches: list[str] = []
    midnight: list[DroppedRow] = []
    zone_defaults: list[str] = []

    def note_if_defaulted(zone: str, owner: str, anchor: str) -> None:
        """A row in the owner's zone that fell back to the default is
        reported, because a row silently in UTC looks right."""
        if zone == default_zone:
            zone_defaults.append(f"{anchor}: owner {owner} has no timezone of their own")

    errors: list[str] = []

    for record in calendar_settings:
        bubble_id = legacy_anchor(record)
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

        rule_zone = owner_zones.get(str(owner), default_zone)
        try:
            start, end = (
                _in_zone(str(raw), zone=rule_zone, export_timezone=export_timezone)
                for raw in (start_raw, end_raw)
            )
        except (TransformError, ValueError) as exc:
            errors.append(f"{bubble_id}: {exc}")
            continue

        if end <= start:
            session_end = _one_session(start, session_minutes.get(str(owner)))
            if session_end is None:
                midnight.append(
                    DroppedRow(bubble_id, f"{start:%H:%M}-{end:%H:%M} does not move forward")
                )
                continue
            end = session_end
            fitted_to_session.append(bubble_id)
        note_if_defaulted(rule_zone, str(owner), bubble_id)
        slot_zone = str(record.get(ZONE_FIELD) or "").strip()
        if slot_zone and slot_zone != rule_zone:
            zone_differs.append(
                f"{bubble_id}: slot says {slot_zone}; owner {owner} is in {rule_zone}"
            )

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
        bubble_id = legacy_anchor(record)
        owner = record.get(CREATOR_FIELD)
        if not owner or str(owner) not in mentor_bubble_ids:
            dropped.append(DroppedRow(bubble_id, "exception has no attributable mentor"))
            continue

        zone = owner_zones.get(str(owner), default_zone)
        note_if_defaulted(zone, str(owner), bubble_id)

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
        source_rule_ids=tuple(legacy_anchor(record) for record in calendar_settings),
        rules=merged_rules,
        exceptions=tuple(exceptions),
        fitted_to_session=tuple(fitted_to_session),
        zone_differs=tuple(zone_differs),
        dropped=tuple(dropped),
        merged_overlaps=merge_notes,
        zone_defaults=tuple(zone_defaults),
        day_mismatches=tuple(day_mismatches),
        midnight_crossing=tuple(midnight),
        errors=tuple(errors) + errors_from_zones,
    )
