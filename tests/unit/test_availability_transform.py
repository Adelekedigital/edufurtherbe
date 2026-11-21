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

ZONES = {MENTOR: "Africa/Lagos", OTHER: "America/New_York"}
MENTORS = {MENTOR, OTHER}


def rule_record(**overrides: Any) -> dict[str, Any]:
    """A canonicalised `CalendarSettings` row, Gen B by default.

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
    {"bubble_id": MENTOR, "Mentor": "m-1", "UserTimezonID": "Africa/Lagos"},
    {"bubble_id": OTHER, "Mentor": "m-2", "UserTimezonID": "America/New_York"},
]


def plan(
    rules: list[dict[str, Any]],
    extra: list[dict[str, Any]] | None = None,
    users: list[dict[str, Any]] | None = None,
    as_of: date = date(2026, 11, 22),
    session_minutes: dict[str, int] | None = None,
) -> Any:
    return plan_availability(
        USERS if users is None else users,
        rules,
        extra or [],
        export_timezone=EXPORT_TIMEZONE,
        as_of=as_of,
        session_minutes=session_minutes or {},
    )


# --------------------------------------------------------------------------
# The two generations — the five-hour question
# --------------------------------------------------------------------------


def test_a_gen_b_row_keeps_the_printed_time() -> None:
    """`timeZone` is set, so the export's rendering IS the declared wall clock.

    Converting here — the obvious move, since `parse_timestamp` is the project's
    blessed parser and returns aware UTC — would store 14:00 for a mentor who
    said 09:00.
    """
    result = plan([rule_record()])

    assert len(result.rules) == 1
    assert (result.rules[0].start_time, result.rules[0].end_time) == (time(9, 0), time(12, 0))
    assert result.rules[0].timezone == "America/New_York"


def test_a_gen_a_row_loads_as_its_utc_moment_in_the_owners_zone() -> None:
    """Decision 359: no `timeZone` on the row, so the stored instant is the
    slot. 426 production bookings fall inside Gen A windows read that way,
    against 142 for the printed reading in the owner's zone.

    Printed 02:00 in the export's zone is 07:00 UTC, which is 08:00 in Lagos.
    """
    result = plan(
        [rule_record(timeZone=None, startTime="Nov 9, 2024 2:00 am", endTime="Nov 9, 2024 2:15 am")]
    )

    assert len(result.rules) == 1
    rule = result.rules[0]
    assert (rule.start_time, rule.end_time) == (time(8, 0), time(8, 15))
    assert rule.timezone == "Africa/Lagos"
    assert result.read_from_utc == ("cs-1",)


def test_a_gen_a_row_takes_the_owners_offset_on_the_as_of_date() -> None:
    """07:00 UTC is 02:00 in New York in winter and 03:00 in summer. The
    cutover date decides it, so every row converts with the same offset."""
    record = rule_record(
        Creator=OTHER, timeZone=None, startTime="Nov 9, 2024 2:00 am", endTime="Nov 9, 2024 3:00 am"
    )

    winter = plan([record]).rules[0]
    summer = plan([record], as_of=date(2026, 7, 1)).rules[0]

    assert (winter.start_time, winter.timezone) == (time(2, 0), "America/New_York")
    assert summer.start_time == time(3, 0)


def test_a_gen_a_row_moves_to_the_owners_weekday() -> None:
    """The legacy weekday is the UTC one. Monday 23:30-00:00 UTC is Tuesday
    00:30-01:00 in Lagos, and a booking on Tuesday there must find it."""
    result = plan(
        [rule_record(timeZone=None, startTime="Nov 9, 2024 6:30 pm", endTime="Nov 9, 2024 7:00 pm")]
    )

    rule = result.rules[0]
    assert (rule.day_of_week, rule.start_time, rule.end_time) == (2, time(0, 30), time(1, 0))


def test_a_zero_length_gen_a_row_is_refused_not_stretched_to_a_day() -> None:
    result = plan(
        [rule_record(timeZone=None, startTime="Nov 9, 2024 9:00 am", endTime="Nov 9, 2024 9:00 am")]
    )

    assert result.rules == ()
    assert "does not move forward" in result.midnight_crossing[0].reason


def test_every_weekday_of_a_zone_takes_one_offset() -> None:
    """New York leaves DST on Sunday 1 Nov 2026. With the cutover on Friday 30
    Oct, a Friday and a Sunday slot at 12:00 UTC must both read 08:00: one
    mentor's hours never sit an hour apart from one day to the next."""
    rows = [
        rule_record(
            bubble_id=anchor,
            Creator=OTHER,
            timeZone=None,
            startTime="Nov 9, 2024 7:00 am",
            endTime="Nov 9, 2024 8:00 am",
            **{"dayOfWeekIn#️⃣": day, "daysOfWeek-O/S🔛": name},
        )
        for anchor, day, name in (("fri", "5", "Friday"), ("sun", "0", "Sunday"))
    ]

    result = plan(rows, as_of=date(2026, 10, 30))

    assert {(r.day_of_week, r.start_time) for r in result.rules} == {
        (5, time(8, 0)),
        (0, time(8, 0)),
    }


def test_windows_in_two_zones_are_never_merged() -> None:
    """The union of 10:00-12:00 Lagos and 11:00-13:00 New York is not
    10:00-13:00 in either; the later one is reported and not loaded."""
    result = plan(
        [
            rule_record(
                bubble_id="lagos",
                timeZone="Africa/Lagos",
                startTime="Dec 15, 2025 10:00 am",
                endTime="Dec 15, 2025 12:00 pm",
            ),
            rule_record(
                bubble_id="ny", startTime="Dec 15, 2025 11:00 am", endTime="Dec 15, 2025 1:00 pm"
            ),
        ]
    )

    assert [(r.legacy_bubble_id, r.end_time) for r in result.rules] == [("lagos", time(12, 0))]
    assert result.merged_overlaps == ()
    assert result.dropped[0].legacy_bubble_id == "ny"
    assert "in Africa/Lagos and this in America/New_York" in result.dropped[0].reason


def test_a_refused_gen_a_row_is_not_reported_as_a_zone_default() -> None:
    result = plan(
        [
            rule_record(
                timeZone=None, startTime="Nov 9, 2024 9:00 am", endTime="Nov 9, 2024 9:00 am"
            )
        ],
        users=[{"bubble_id": MENTOR, "Mentor": "m-1"}],
    )

    assert result.midnight_crossing and result.zone_defaults == ()


def test_the_discriminator_reads_none_not_empty_string() -> None:
    """`blank_to_none` runs during canonicalisation, so a blank column arrives as
    `None`. A `== ""` test would classify every row as Gen B and apply the wrong
    time rule to half the table — the same five-hour defect, one line away."""
    as_none = plan([rule_record(timeZone=None)])
    as_blank = plan([rule_record(timeZone="")])

    assert as_none.read_from_utc == as_blank.read_from_utc == ("cs-1",)
    assert as_none.rules == as_blank.rules
    assert as_none.rules[0].timezone == "Africa/Lagos"


def test_a_gen_a_row_whose_owner_has_no_zone_lands_in_utc_and_is_reported() -> None:
    result = plan(
        [
            rule_record(
                timeZone=None, startTime="Nov 9, 2024 2:00 am", endTime="Nov 9, 2024 2:15 am"
            )
        ],
        users=[{"bubble_id": MENTOR, "Mentor": "m-1"}],
    )

    assert (result.rules[0].start_time, result.rules[0].timezone) == (time(7, 0), "UTC")
    assert "has no timezone of their own" in result.zone_defaults[0]


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


def test_a_gen_a_window_crossing_midnight_in_utc_loads_in_the_owners_zone() -> None:
    """23:30-00:30 UTC crosses midnight, but each end converts on its own, so
    in New York it is 18:30-19:30 and loads. The ordering check runs on the
    reading that is stored, never on the UTC one."""
    result = plan(
        [
            rule_record(
                Creator=OTHER,
                timeZone=None,
                startTime="Nov 9, 2024 6:30 pm",
                endTime="Nov 9, 2024 7:30 pm",
            )
        ]
    )

    assert result.midnight_crossing == ()
    assert (result.rules[0].start_time, result.rules[0].end_time) == (time(18, 30), time(19, 30))


def test_a_gen_a_window_crossing_midnight_in_the_owners_zone_is_reported() -> None:
    """22:30-23:30 UTC is 23:30-00:30 in Lagos: not a window the column holds."""
    result = plan(
        [rule_record(timeZone=None, startTime="Nov 9, 2024 5:30 pm", endTime="Nov 9, 2024 6:30 pm")]
    )

    assert result.rules == ()
    assert "23:30-00:30 does not move forward" in result.midnight_crossing[0].reason


def test_a_gen_b_row_with_a_broken_end_is_read_from_its_display_text() -> None:
    """Production: a Sunday 1:00-2:45 PM slot whose `endTime` carries another
    date and reads 03:00. The 24-hour text is what the legacy app showed the
    mentor, so it is used when, and only when, the stamps do not move forward."""
    result = plan(
        [
            rule_record(
                startTime="Mar 31, 2000 1:00 pm",
                endTime="Nov 6, 2026 3:00 am",
                **{"24hr-locatStartTime-TXT": "13:00", "24hr-localEndTime-TXT": "14:45"},
            )
        ]
    )

    assert (result.rules[0].start_time, result.rules[0].end_time) == (time(13, 0), time(14, 45))
    assert result.repaired_from_text == ("cs-1",)
    assert result.midnight_crossing == ()


def test_display_text_that_does_not_move_forward_either_is_still_reported() -> None:
    result = plan(
        [
            rule_record(
                startTime="Mar 31, 2000 7:00 pm",
                endTime="Mar 31, 2000 7:45 am",
                **{"24hr-locatStartTime-TXT": "19:00", "24hr-localEndTime-TXT": "07:45"},
            )
        ]
    )

    assert result.rules == () and result.repaired_from_text == ()
    assert "does not move forward" in result.midnight_crossing[0].reason


def test_display_text_on_a_healthy_row_is_ignored() -> None:
    """The text is stale on rows a mentor later edited; the stamps win."""
    result = plan(
        [rule_record(**{"24hr-locatStartTime-TXT": "12:30", "24hr-localEndTime-TXT": "13:45"})]
    )

    assert (result.rules[0].start_time, result.rules[0].end_time) == (time(9, 0), time(12, 0))
    assert result.repaired_from_text == ()


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


def test_an_unrecognised_timezone_is_refused_rather_than_stored() -> None:
    """`availability_rules.timezone` is `text` with no CHECK — PostgreSQL will
    not accept `pg_timezone_names` in one — so this is the only place the value
    is checked at all. Left unvalidated it passes NOT NULL and then raises
    `ZoneInfoNotFoundError` inside the projection, long after the ETL exited 0.
    """
    result = plan([rule_record(timeZone="Eastern Standard Time")])

    assert result.rules == ()
    assert "is not an IANA zone" in result.dropped[0].reason


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
    `users.timezone` is the only source. Rules never reach this path: a rule
    without a zone is Gen A, read from its UTC moment in the owner's zone."""
    result = plan(
        [],
        [{"bubble_id": "ce-1", "Creator": MENTOR, "block-Date(s)": "Jan 13, 2025 12:00 am"}],
    )

    assert result.exceptions[0].timezone == "Africa/Lagos"
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

    It says how many rules were read from their UTC moment, and it is the only
    place a merged or dropped row is ever mentioned. An untested renderer that
    quietly stops printing one of those sections would leave the run looking
    clean.
    """
    result = plan(
        [
            rule_record(bubble_id="loaded"),
            rule_record(bubble_id="genA", timeZone=None, Creator=OTHER),
            rule_record(bubble_id="orphan", Creator=None),
        ],
        [{"bubble_id": "ce-1", "Creator": MENTOR, "block-Date(s)": "Jan 13, 2025 12:00 am"}],
    )
    text = result.report()

    assert "rules loaded        2" in text
    assert "exceptions loaded   1" in text
    assert "read from UTC       1" in text
    assert "dropped             1" in text
    assert "read from UTC moment (no slot zone) (1):\n  genA" in text


def test_the_report_is_quiet_when_there_is_nothing_to_say() -> None:
    """A clean run must not print empty sections — an operator reading a report
    full of zero-length headings stops reading reports."""
    text = plan([rule_record()]).report()

    assert "rules loaded        1" in text
    assert "dropped (" not in text
    assert "read from UTC moment" not in text


def test_timestamps_come_from_bubble_not_the_import_clock() -> None:
    """Settled decision #29: Creation Date lands in `created_at`. The loader
    disables the trigger so the value survives; this asserts the transform
    carries it that far."""
    result = plan([rule_record()])

    assert result.rules[0].created_at == datetime(2025, 12, 15, 14, 0, tzinfo=UTC)
