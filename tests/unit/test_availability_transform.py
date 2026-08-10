"""The calendar transform: what loads, what is quarantined, what is refused.

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
) -> Any:
    return plan_availability(
        USERS if users is None else users,
        rules,
        extra or [],
        export_timezone=EXPORT_TIMEZONE,
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
    assert result.quarantined == ()


def test_a_gen_a_row_is_quarantined_with_both_readings() -> None:
    """No `timeZone`, so which reading is real cannot be settled from this data.

    The displayed value is the UTC rendering, five hours from the printed one —
    measured against the legacy `12hr-…-TXT` column on every Gen A row in the
    export.
    """
    result = plan(
        [rule_record(timeZone=None, startTime="Nov 9, 2024 2:00 am", endTime="Nov 9, 2024 2:15 am")]
    )

    assert result.rules == ()
    assert len(result.quarantined) == 1
    held = result.quarantined[0]
    assert held.as_printed == time(2, 0)
    assert held.as_displayed == time(7, 0)
    assert held.end_as_printed == time(2, 15)
    assert held.end_as_displayed == time(7, 15)


def test_the_discriminator_reads_none_not_empty_string() -> None:
    """`blank_to_none` runs during canonicalisation, so a blank column arrives as
    `None`. A `== ""` test would classify every row as Gen B and apply the wrong
    time rule to half the table — the same five-hour defect, one line away."""
    as_none = plan([rule_record(timeZone=None)])
    as_blank = plan([rule_record(timeZone="")])

    assert len(as_none.quarantined) == 1
    assert len(as_blank.quarantined) == 1
    assert as_none.rules == as_blank.rules == ()


# --------------------------------------------------------------------------
# Rows that cannot become rows
# --------------------------------------------------------------------------


def test_a_row_with_no_creator_is_dropped() -> None:
    result = plan([rule_record(Creator=None)])

    assert result.rules == () and result.quarantined == ()
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


def test_a_gen_a_evening_window_still_reaches_the_quarantine() -> None:
    """Found by review. The ordering check used to run before the quarantine
    branch, on the Gen A *displayed* reading — so a window printed 18:30-19:30
    shifted to 23:30-00:30, failed `end > start`, and was reported as a
    midnight-crossing window nobody entered instead of reaching the quarantine.

    That block is what the production reading is decided from, so a row leaving
    it silently is worse than a wrong time: nobody knows to ask about it. Dev's
    Gen A rows all sit at 00:45-21:30 so both ends shift together — luck, not
    coverage.
    """
    result = plan(
        [
            rule_record(
                timeZone=None,
                startTime="Nov 9, 2024 6:30 pm",
                endTime="Nov 9, 2024 7:30 pm",
            )
        ]
    )

    assert len(result.quarantined) == 1
    assert result.midnight_crossing == ()
    assert result.quarantined[0].as_printed == time(18, 30)


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
    without a zone is Gen A by definition, and Gen A is quarantined."""
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


def test_quarantined_mentors_are_named_as_needing_to_redeclare() -> None:
    """The list that makes the availability write surface a cutover dependency.

    A mentor whose only rules are quarantined arrives with an empty schedule. A
    mentor who also has a loaded rule does not, so they are excluded.
    """
    result = plan(
        [
            rule_record(bubble_id="a", Creator=MENTOR, timeZone=None),
            rule_record(bubble_id="b", Creator=OTHER),
        ]
    )

    assert result.mentors_needing_redeclaration() == (MENTOR,)


def test_the_report_states_every_outcome_a_run_can_have() -> None:
    """The report **is** the deliverable, not a log line.

    It is what the Gen A reading gets decided from at the production extract, it
    is what tells an operator which mentors arrive with an empty schedule, and
    it is the only place a merged or dropped row is ever mentioned. An untested
    renderer that quietly stops printing one of those sections would leave the
    run looking clean.
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

    assert "rules loaded        1" in text
    assert "exceptions loaded   1" in text
    assert "quarantined (Gen A) 1 across 1 mentors" in text
    assert "dropped             1" in text
    # Both readings, side by side — a comparison rather than a re-derivation.
    assert "printed" in text and "displayed" in text
    assert "09:00-12:00" in text and "14:00-17:00" in text
    # And the cutover consequence, named.
    assert "must re-declare it at cutover" in text
    assert OTHER in text


def test_the_report_is_quiet_when_there_is_nothing_to_say() -> None:
    """A clean run must not print empty sections — an operator reading a report
    full of zero-length headings stops reading reports."""
    text = plan([rule_record()]).report()

    assert "rules loaded        1" in text
    assert "dropped (" not in text
    assert "quarantined Gen A rules" not in text
    assert "must re-declare" not in text


def test_timestamps_come_from_bubble_not_the_import_clock() -> None:
    """Settled decision #29: Creation Date lands in `created_at`. The loader
    disables the trigger so the value survives; this asserts the transform
    carries it that far."""
    result = plan([rule_record()])

    assert result.rules[0].created_at == datetime(2025, 12, 15, 14, 0, tzinfo=UTC)
