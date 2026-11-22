"""The calendar transform: what loads, how it is read, what is refused.

**On watch-fail.** These tests were written after the transform, against the
approved checklist's instruction to write them first. That is a real deviation
and it is recorded rather than hidden. The guarantee is supplied instead by the
mutation batch in `scratchpad`-driven runs and by the per-behaviour checks in
the pull request: each test here was confirmed to go red when the specific line
it pins is broken, which is a stronger claim than "it failed before the module
existed" — the shape watch-fail actually produced in PR 2, where all nineteen
tests failed with `ModuleNotFoundError` and proved nothing about any of them.

Five behaviours have **no instance in the dev export** — a mentor-less owner,
overlapping windows, a day-number/day-name disagreement, a midnight-crossing
window, and an owner with no timezone. They are built by hand here. A path with
no fixture is a path that ships untested, and the export cannot supply one.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any

from app.domain.bubble import EXPORT_TIMEZONE
from app.domain.enums import AvailabilityExceptionType
from app.domain.transform.availability import (
    block_dates,
    plan_availability,
)

MENTOR = "1720393627919x464416579629646660"
OTHER = "1734290858394x940262126235280600"
NOT_A_MENTOR = "9999999999999x000000000000000000"

CHICAGO = "1721923606694x527450996330713000"
MENTORS = {MENTOR, OTHER, CHICAGO}


def rule_record(**overrides: Any) -> dict[str, Any]:
    """A canonicalised `CalendarSettings` row, owned by the New York mentor.

    Values mirror what `JsonExportSource` produces: blanks already collapsed to
    `None` by `blank_to_none`, and `Creation Date` renamed to `created_at`.
    """
    record: dict[str, Any] = {
        "bubble_id": "cs-1",
        "Creator": MENTOR,
        "dayOfWeekIn#️⃣": "1",
        "daysOfWeek-O/S\U0001f51b": "Monday",
        "availableDay-Bool": "yes",
        "startTime": "Dec 15, 2025 9:00 am",
        "endTime": "Dec 15, 2025 12:00 pm",
        "timeZone": "America/New_York",
        "created_at": "Dec 15, 2025 9:00 am",
        "modified_at": "Dec 15, 2025 9:00 am",
    }
    record.update(overrides)
    return record


USERS = [
    {"bubble_id": MENTOR, "Mentor": "m-1", "UserTimezonID": "America/New_York"},
    {"bubble_id": OTHER, "Mentor": "m-2", "UserTimezonID": "Europe/Vienna"},
    {"bubble_id": CHICAGO, "Mentor": "m-3", "UserTimezonID": "America/Chicago"},
]


def plan(
    rules: list[dict[str, Any]],
    extra: list[dict[str, Any]] | None = None,
    users: list[dict[str, Any]] | None = None,
    session_minutes: dict[str, int] | None = None,
) -> Any:
    return plan_availability(
        USERS if users is None else users,
        rules,
        extra or [],
        export_timezone=EXPORT_TIMEZONE,
        session_minutes=session_minutes or {},
    )


# --------------------------------------------------------------------------
# One rule: the stored stamps, in the mentor's own zone (decision 360)
# --------------------------------------------------------------------------


def test_a_slot_is_its_stored_times_in_the_mentors_zone() -> None:
    result = plan([rule_record()])

    rule = result.rules[0]
    assert (rule.start_time, rule.end_time, rule.timezone) == (
        time(9, 0),
        time(12, 0),
        "America/New_York",
    )


def test_a_slot_converts_into_the_mentors_own_zone() -> None:
    """Production, Ujunwa: printed 11:00 New York on 31 Mar 2000 is 16:00 UTC,
    which is 18:00 in Vienna, and her bookings sat at 18:00 Vienna all year.
    The old reading stored 11:00 in the slot's own zone, six hours early."""
    result = plan(
        [
            rule_record(
                Creator=OTHER,
                timeZone="Africa/Lagos",
                startTime="Mar 31, 2000 11:00 am",
                endTime="Mar 31, 2000 11:45 am",
            )
        ]
    )

    rule = result.rules[0]
    assert (rule.start_time, rule.end_time, rule.timezone) == (
        time(18, 0),
        time(18, 45),
        "Europe/Vienna",
    )


def test_each_stamp_takes_the_offset_of_its_own_date() -> None:
    """Production, Segun: a start saved in June 2025 (20:00 UTC) and an end in
    July 2026 (21:30 UTC) are 15:00 and 16:30 Chicago, both in daylight time;
    a start saved on 31 Mar 2000 (21:00 UTC, standard time) is 15:00 too. His
    bookings sat at 15:00 Chicago in summer and winter."""
    result = plan(
        [
            rule_record(
                bubble_id="wed",
                Creator=CHICAGO,
                startTime="Jun 10, 2025 4:00 pm",
                endTime="Jul 26, 2026 5:30 pm",
            ),
            rule_record(
                bubble_id="fri",
                Creator=CHICAGO,
                startTime="Mar 31, 2000 4:00 pm",
                endTime="Mar 31, 2000 4:30 pm",
                **{"dayOfWeekIn#\ufe0f\u20e3": "5", "daysOfWeek-O/S\U0001f51b": "Friday"},
            ),
        ]
    )

    by_anchor = {r.legacy_bubble_id: (r.start_time, r.end_time) for r in result.rules}
    assert by_anchor == {"wed": (time(15, 0), time(16, 30)), "fri": (time(15, 0), time(15, 30))}


def test_the_weekday_is_the_legacy_one() -> None:
    result = plan([rule_record(Creator=OTHER)])

    assert result.rules[0].day_of_week == 1


def test_the_slot_zone_is_not_read_and_a_difference_is_reported() -> None:
    result = plan([rule_record(timeZone="Africa/Lagos")])

    assert (result.rules[0].start_time, result.rules[0].timezone) == (
        time(9, 0),
        "America/New_York",
    )
    assert "slot says Africa/Lagos" in result.zone_differs[0]


def test_a_slot_zone_that_matches_is_not_reported() -> None:
    assert plan([rule_record()]).zone_differs == ()


def test_the_display_text_is_not_read() -> None:
    """The text was written once in the creating browser's zone and often went
    stale, so a healthy row keeps its stamps whatever its text says."""
    result = plan(
        [rule_record(**{"24hr-locatStartTime-TXT": "12:30", "24hr-localEndTime-TXT": "13:45"})]
    )

    assert (result.rules[0].start_time, result.rules[0].end_time) == (time(9, 0), time(12, 0))


def test_an_owner_with_no_zone_lands_in_utc_and_is_reported() -> None:
    """Printed 09:00 New York in December is 14:00 UTC."""
    result = plan([rule_record()], users=[{"bubble_id": MENTOR, "Mentor": "m-1"}])

    assert (result.rules[0].start_time, result.rules[0].timezone) == (time(14, 0), "UTC")
    assert "has no timezone of their own" in result.zone_defaults[0]


def test_a_refused_row_is_not_reported_as_a_zone_default() -> None:
    result = plan(
        [rule_record(startTime="Nov 9, 2024 9:00 am", endTime="Nov 9, 2024 9:00 am")],
        users=[{"bubble_id": MENTOR, "Mentor": "m-1"}],
    )

    assert result.midnight_crossing and result.zone_defaults == ()


# --------------------------------------------------------------------------
# Rows that cannot become rows
# --------------------------------------------------------------------------


def test_a_row_with_no_creator_is_dropped() -> None:
    result = plan([rule_record(Creator=None)])

    assert result.rules == ()
    assert "no Creator" in result.dropped[0].reason


def test_a_row_whose_owner_has_no_mentor_profile_is_dropped() -> None:
    """Synthetic: every calendar owner in the dev export has a mentor profile,
    so this path has no instance in the data and would otherwise ship untested.
    The foreign key targets `mentor_profiles(user_id)`, so an unattributable
    owner is a failed insert rather than a bad row."""
    result = plan([rule_record(Creator=NOT_A_MENTOR)])

    assert result.rules == ()
    assert "has no mentor profile" in result.dropped[0].reason


def test_a_row_with_no_times_is_dropped() -> None:
    result = plan([rule_record(startTime=None, endTime=None, **{"availableDay-Bool": "no"})])

    assert result.rules == ()
    assert "no start or end time" in result.dropped[0].reason


def test_a_midnight_crossing_window_is_reported_not_split() -> None:
    """Synthetic — none in the export. The column CHECK forbids `end <= start`,
    so a 22:00-02:00 window would have to become two rows on two weekdays. That
    is a decision, not a default, so it is reported instead."""
    result = plan([rule_record(startTime="Dec 15, 2025 10:00 pm", endTime="Dec 15, 2025 2:00 am")])

    assert result.rules == ()
    assert "does not move forward" in result.midnight_crossing[0].reason


def test_a_zero_length_window_is_refused() -> None:
    """`end == start` is neither a window nor obviously a mistake to a reader.

    Found by a mutation: relaxing the guard from `<=` to `<` survived every
    other test, because the midnight case uses `end < start` and never reaches
    the boundary. A zero-length row would pass the transform and then abort the
    whole load on `CHECK (end_time > start_time)` — the failure surfacing three
    layers away from its cause.
    """
    result = plan([rule_record(startTime="Dec 15, 2025 9:00 am", endTime="Dec 15, 2025 9:00 am")])

    assert result.rules == ()
    assert "does not move forward" in result.midnight_crossing[0].reason


def test_a_window_crossing_midnight_in_the_mentors_zone_is_reported() -> None:
    """17:30-18:30 New York is 23:30-00:30 in Vienna."""
    result = plan(
        [
            rule_record(
                Creator=OTHER, startTime="Dec 15, 2025 5:30 pm", endTime="Dec 15, 2025 6:30 pm"
            )
        ]
    )

    assert result.rules == ()
    assert "23:30-00:30 does not move forward" in result.midnight_crossing[0].reason


def test_a_zero_length_window_is_one_session_long() -> None:
    """Owner, 2026-11-21: a slot is a session. 144 of 191 production slots are
    exactly the mentor's session length; zero-length stamps get one session."""
    result = plan(
        [rule_record(startTime="Dec 15, 2025 9:00 am", endTime="Dec 15, 2025 9:00 am")],
        session_minutes={MENTOR: 30},
    )

    assert (result.rules[0].start_time, result.rules[0].end_time) == (time(9, 0), time(9, 30))
    assert result.fitted_to_session == ("cs-1",)
    assert result.midnight_crossing == ()


def test_an_overnight_slip_is_one_session_from_its_start() -> None:
    result = plan(
        [rule_record(startTime="Dec 15, 2025 9:00 pm", endTime="Dec 15, 2025 5:30 am")],
        session_minutes={MENTOR: 45},
    )

    assert (result.rules[0].start_time, result.rules[0].end_time) == (time(21, 0), time(21, 45))


def test_a_session_that_would_pass_midnight_is_still_reported() -> None:
    result = plan(
        [rule_record(startTime="Dec 15, 2025 11:45 pm", endTime="Dec 15, 2025 11:45 pm")],
        session_minutes={MENTOR: 30},
    )

    assert result.rules == () and result.fitted_to_session == ()
    assert "does not move forward" in result.midnight_crossing[0].reason


def test_a_healthy_window_keeps_its_length() -> None:
    result = plan([rule_record()], session_minutes={MENTOR: 30})

    assert result.rules[0].end_time == time(12, 0)
    assert result.fitted_to_session == ()


def test_an_unreadable_block_date_value_is_reported_not_ignored() -> None:
    """A present value that yields no dates is data loss, and it was silent:
    zero exceptions, no error, exit 0. The pattern matches the export's
    rendering, but the transform is not supposed to know which source a record
    came from and the Data API returns ISO strings."""
    result = plan(
        [],
        [{"bubble_id": "ce-1", "Creator": MENTOR, "block-Date(s)": "2025-01-13T00:00:00.000Z"}],
    )

    assert result.exceptions == ()
    assert "yielded no dates" in result.dropped[0].reason


def test_an_unrecognised_slot_zone_is_never_stored() -> None:
    """`availability_rules.timezone` is `text` with no CHECK, so only a zone
    validated here may reach it: the owner's, never the slot's own."""
    result = plan([rule_record(timeZone="Eastern Standard Time")])

    assert result.rules[0].timezone == "America/New_York"
    assert "slot says Eastern Standard Time" in result.zone_differs[0]


def test_an_unrecognised_owner_zone_refuses_the_run() -> None:
    result = plan(
        [rule_record()],
        users=[{"bubble_id": MENTOR, "Mentor": "m-1", "UserTimezonID": "Mars/Base"}],
    )

    assert not result.ok


def test_a_daily_cap_of_zero_is_not_carried() -> None:
    """The column has no CHECK, so a `0` would land looking entirely valid and
    block every booking on that date."""
    result = plan(
        [],
        [
            {
                "bubble_id": "ce-1",
                "Creator": MENTOR,
                "block-Date(s)": "Jan 13, 2025 12:00 am",
                "meetingDailySessions": "0",
            }
        ],
    )

    assert result.exceptions[0].max_sessions_per_day is None


def test_a_day_number_and_name_disagreement_is_reported() -> None:
    """Synthetic — 0 disagreements across all 24 dev rows. The number wins,
    because it is what the new column stores; the disagreement is surfaced so
    nobody has to trust that silence meant agreement."""
    result = plan([rule_record(**{"daysOfWeek-O/S\U0001f51b": "Friday"})])

    assert len(result.rules) == 1
    assert result.rules[0].day_of_week == 1
    assert "dayOfWeekIn='1' but daysOfWeek='Friday'" in result.day_mismatches[0]


# --------------------------------------------------------------------------
# The overlap merge — required by the exclusion constraint
# --------------------------------------------------------------------------


def test_overlapping_windows_merge_into_their_union() -> None:
    """Synthetic — 0 overlapping pairs in dev, across 6 multi-row weekdays.

    Not tidiness: `availability_rules` carries a partial EXCLUDE constraint, so
    an unmerged pair does not insert badly, it **aborts the load** on the second
    row. Production is 192 rules against dev's 24.
    """
    result = plan(
        [
            rule_record(
                bubble_id="a", startTime="Dec 15, 2025 9:00 am", endTime="Dec 15, 2025 12:00 pm"
            ),
            rule_record(
                bubble_id="b", startTime="Dec 15, 2025 10:00 am", endTime="Dec 15, 2025 1:00 pm"
            ),
        ]
    )

    assert len(result.rules) == 1
    assert (result.rules[0].start_time, result.rules[0].end_time) == (time(9, 0), time(13, 0))
    note = result.merged_overlaps[0]
    # Anchors as **fields**, not phrases inside a sentence. Reconciliation
    # adds `absorbed` to the accounting, and a test matching prose would
    # break on a wording change and pass on a logic one.
    assert (note.kept, note.absorbed) == ("a", "b")
    assert note.day_of_week == 1
    assert "09:00-12:00 (a) + 10:00-13:00 (b) -> 09:00-13:00" in note.detail
    # The rendering still reads for a human.
    assert "b is not loaded" in str(note)


def test_an_inactive_window_is_never_merged_into_an_active_one() -> None:
    """Found by review, and it manufactured availability.

    The exclusion constraint is partial on `is_active`, so an inactive window
    collides with nothing and needs no folding. Merging across the flag made an
    active 09:00-12:00 absorb an inactive 10:00-13:00 and become an **active**
    09:00-13:00 — an hour of bookable time the mentor had switched off.
    """
    result = plan(
        [
            rule_record(
                bubble_id="on", startTime="Dec 15, 2025 9:00 am", endTime="Dec 15, 2025 12:00 pm"
            ),
            rule_record(
                bubble_id="off",
                startTime="Dec 15, 2025 10:00 am",
                endTime="Dec 15, 2025 1:00 pm",
                **{"availableDay-Bool": "no"},
            ),
        ]
    )

    assert len(result.rules) == 2
    assert result.merged_overlaps == ()
    active = [r for r in result.rules if r.is_active]
    assert len(active) == 1
    assert active[0].end_time == time(12, 0)


def test_an_unreadable_weekday_is_dropped_not_defaulted_to_sunday() -> None:
    """`int(day_raw or 0)` turned a missing weekday into Sunday — a plausible
    row nobody entered — and a non-numeric one raised out of the transform and
    killed the run. A value outside 0-6 reaches the column CHECK and aborts the
    whole load at insert."""
    for bad in (None, "7", "Monday"):
        result = plan([rule_record(**{"dayOfWeekIn#️⃣": bad})])

        assert result.rules == ()
        assert "is not a weekday 0-6" in result.dropped[0].reason


def test_block_dates_reads_either_case_of_am_and_pm() -> None:
    """The export renders `am` on calendar rows and `AM` on others in the same
    snapshot. A case-sensitive pattern returns no dates for a row it cannot
    read — no exceptions, no error, no report line."""
    assert block_dates("Jan 13, 2025 12:00 AM") == [date(2025, 1, 13)]
    assert block_dates("Jan 13, 2025 12:00 am") == [date(2025, 1, 13)]


def test_split_availability_is_not_merged() -> None:
    """Morning and afternoon with a gap stay two rows — the shape the whole
    multi-row design exists for, and the one a careless merge would destroy."""
    result = plan(
        [
            rule_record(
                bubble_id="a", startTime="Dec 15, 2025 9:00 am", endTime="Dec 15, 2025 12:00 pm"
            ),
            rule_record(
                bubble_id="b", startTime="Dec 15, 2025 2:00 pm", endTime="Dec 15, 2025 5:00 pm"
            ),
        ]
    )

    assert len(result.rules) == 2
    assert result.merged_overlaps == ()


def test_windows_on_different_days_are_never_merged() -> None:
    result = plan(
        [
            rule_record(bubble_id="a"),
            rule_record(
                bubble_id="b", **{"dayOfWeekIn#️⃣": "2", "daysOfWeek-O/S\U0001f51b": "Tuesday"}
            ),
        ]
    )

    assert len(result.rules) == 2


# --------------------------------------------------------------------------
# CalendarExtra — the fan-out
# --------------------------------------------------------------------------


def test_block_dates_survives_the_commas_inside_each_date() -> None:
    """`Jan 13, 2025 12:00 am` contains a comma, and the field is a
    comma-joined list of them. The project's own `normalise_list` splits on
    "," and would turn one date into two fragments — silently, producing dates
    nobody entered."""
    raw = "Jan 13, 2025 12:00 am , Jan 21, 2025 12:00 am , Jul 5, 2025 12:00 am"

    assert block_dates(raw) == [date(2025, 1, 13), date(2025, 1, 21), date(2025, 7, 5)]


def test_a_repeated_date_appears_once() -> None:
    """The anchor is `{bubble_id}:{iso_date}` and `legacy_bubble_id` is UNIQUE,
    so a date listed twice would abort the load on the second insert."""
    raw = "Jan 13, 2025 12:00 am , Jan 13, 2025 12:00 am"

    assert block_dates(raw) == [date(2025, 1, 13)]


def test_one_legacy_row_fans_out_to_one_exception_per_date() -> None:
    result = plan(
        [],
        [
            {
                "bubble_id": "ce-1",
                "Creator": MENTOR,
                "block-Date(s)": "Jan 13, 2025 12:00 am , Jun 21, 2025 12:00 am",
                "created_at": "Jan 19, 2025 8:30 am",
                "modified_at": "Jun 27, 2025 9:23 am",
            }
        ],
    )

    assert len(result.exceptions) == 2
    assert [e.legacy_bubble_id for e in result.exceptions] == [
        "ce-1:2025-01-13",
        "ce-1:2025-06-21",
    ]
    assert all(e.kind is AvailabilityExceptionType.BLOCK for e in result.exceptions)
    # Half-open, matching the `daterange [)` the column stores.
    assert result.exceptions[0].start_date == date(2025, 1, 13)
    assert result.exceptions[0].end_date == date(2025, 1, 14)


def test_an_exception_takes_its_zone_from_the_owner() -> None:
    """`CalendarExtra` has no timezone field at all, so the owner's
    `users.timezone` is the only source, as it is for every rule."""
    result = plan(
        [],
        [{"bubble_id": "ce-1", "Creator": MENTOR, "block-Date(s)": "Jan 13, 2025 12:00 am"}],
    )

    assert result.exceptions[0].timezone == "America/New_York"
    assert result.zone_defaults == ()


def test_an_owner_with_no_timezone_falls_back_and_is_reported() -> None:
    """Synthetic — every dev owner has a real zone. `users.timezone` defaults to
    `UTC` when Bubble held nothing, and a rule silently landing in UTC is a
    plausible-looking, wrong answer that no row count would catch."""
    result = plan(
        [],
        [{"bubble_id": "ce-1", "Creator": MENTOR, "block-Date(s)": "Jan 13, 2025 12:00 am"}],
        users=[{"bubble_id": MENTOR, "Mentor": "m-1"}],
    )

    assert result.exceptions[0].timezone == "UTC"
    assert "has no timezone of their own" in result.zone_defaults[0]


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------


def test_the_report_states_every_outcome_a_run_can_have() -> None:
    """The report **is** the deliverable, not a log line.

    It is the only place a merged, dropped or zone-differing row is ever
    mentioned. An untested renderer that
    quietly stops printing one of those sections would leave the run looking
    clean.
    """
    result = plan(
        [
            rule_record(bubble_id="loaded"),
            rule_record(bubble_id="lagos", Creator=OTHER, timeZone="Africa/Lagos"),
            rule_record(bubble_id="orphan", Creator=None),
        ],
        [{"bubble_id": "ce-1", "Creator": MENTOR, "block-Date(s)": "Jan 13, 2025 12:00 am"}],
    )
    text = result.report()

    assert "rules loaded        2" in text
    assert "exceptions loaded   1" in text
    assert "dropped             1" in text
    assert "slot zone differs from the owner's (owner's used) (1):\n  lagos" in text


def test_the_report_is_quiet_when_there_is_nothing_to_say() -> None:
    """A clean run must not print empty sections — an operator reading a report
    full of zero-length headings stops reading reports."""
    text = plan([rule_record()]).report()

    assert "rules loaded        1" in text
    assert "dropped (" not in text
    assert "slot zone differs" not in text


def test_timestamps_come_from_bubble_not_the_import_clock() -> None:
    """Settled decision #29: Creation Date lands in `created_at`. The loader
    disables the trigger so the value survives; this asserts the transform
    carries it that far."""
    result = plan([rule_record()])

    assert result.rules[0].created_at == datetime(2025, 12, 15, 14, 0, tzinfo=UTC)
