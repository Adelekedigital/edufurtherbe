"""Who is asked whether they are free, and what their answer blocks.

**A cancelled hour goes back on the grid.** It did not, and that was a defect of
the same family as `withdrawn`: an hour hidden from the grid that
`sessions_no_mentor_double_booking` — built over `LIVE_STATUSES`, which excludes
`cancelled` — would have accepted a booking for anyway, with nothing anywhere
able to release it.

**Unavailability is an availability exception**, which is the mechanism that
already means it. So the interesting half here is the conversion: a session
knows a UTC instant and a length, and that table stores local dates and local
times. Getting that wrong is invisible in the response and wrong on somebody's
calendar.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from app.core.config import Settings
from app.domain.availability import unavailable_windows
from app.domain.enums import SessionRole, SessionStatus
from app.domain.sessions import records_unavailability
from app.infra.db.models.sessions import FREES_THE_HOUR, LIVE_STATUSES
from app.main import create_app

LAGOS = "Africa/Lagos"  # No DST, ever — so a local time can be written down.
NEW_YORK = "America/New_York"


def utc(*args: int) -> dt.datetime:
    return dt.datetime(*args, tzinfo=dt.UTC)  # type: ignore[arg-type]


def resolved(date: dt.date, moment: dt.time, timezone: str) -> dt.datetime:
    """The instant a stored window resolves to, the way the projection does it."""
    return dt.datetime.combine(date, moment.replace(fold=0), tzinfo=ZoneInfo(timezone)).astimezone(
        dt.UTC
    )


# --------------------------------------------------------------------------
# Who is asked
# --------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["accept", "decline", "withdraw"])
def test_only_cancelling_can_record_unavailability(action: str) -> None:
    """Nothing else ends an agreement, so nothing else has an hour to protect."""
    assert not records_unavailability(action, SessionRole.MENTOR, release_slot=False)


def test_a_mentor_who_says_they_are_not_free_blocks_the_time() -> None:
    assert records_unavailability("cancel", SessionRole.MENTOR, release_slot=False)


def test_a_mentor_who_says_they_are_free_blocks_nothing() -> None:
    assert not records_unavailability("cancel", SessionRole.MENTOR, release_slot=True)


def test_a_mentee_can_never_hold_a_mentors_hour() -> None:
    """**The defect, in the one place it could come back.**

    A mentee cancelling says nothing about whether the mentor is free. Honouring
    their answer would let book-and-cancel empty a calendar an hour at a time —
    which is what `withdrawn` once did, wearing a different status.
    """
    assert not records_unavailability("cancel", SessionRole.MENTEE, release_slot=False)


# --------------------------------------------------------------------------
# What gets blocked
# --------------------------------------------------------------------------


def test_an_ordinary_session_is_one_window_in_the_mentors_own_day() -> None:
    """08:00Z is 09:00 in Lagos, which never observes DST."""
    (window,) = unavailable_windows(
        starts_at=utc(2026, 9, 1, 8, 0), duration_minutes=60, timezone=LAGOS
    )

    assert window.date == dt.date(2026, 9, 1)
    assert window.start_time == dt.time(9, 0)
    assert window.end_time == dt.time(10, 0)


def test_a_session_crossing_local_midnight_becomes_two_windows() -> None:
    """**Reachable, not theoretical.** A declared window cannot cross midnight —
    `availability_window_ordered` forbids it — but that CHECK is in the *rule's*
    timezone, and nothing requires a rule's zone to be the mentor's own. Unsplit,
    the row would violate that same CHECK and the mentor could not cancel at all.
    """
    first, second = unavailable_windows(
        starts_at=utc(2026, 9, 1, 22, 30), duration_minutes=90, timezone=LAGOS
    )

    assert (first.date, first.start_time) == (dt.date(2026, 9, 1), dt.time(23, 30))
    assert first.end_time == dt.time.max
    assert (second.date, second.start_time, second.end_time) == (
        dt.date(2026, 9, 2),
        dt.time(0, 0),
        dt.time(1, 0),
    )


def test_a_session_ending_exactly_at_midnight_writes_no_empty_second_row() -> None:
    """`time.max` rather than a `24:00` that does not exist.

    It leaves the final microsecond of the day unblocked, which no slot can
    occupy — the shortest offering is minutes long. Blocking the whole of the
    next day instead would be a real cost to a mentor.
    """
    windows = unavailable_windows(
        starts_at=utc(2026, 9, 1, 22, 0), duration_minutes=60, timezone=LAGOS
    )

    assert len(windows) == 1
    assert windows[0].end_time == dt.time.max


def test_the_blocked_time_is_the_session_time_across_a_dst_transition() -> None:
    """**The round trip is the assertion.** Local times are worth writing down,
    but what matters is that resolving them back yields the hour the session
    actually occupied — and a wall-clock `+ timedelta` would silently drift by an
    hour here, where New York moves 02:00 to 03:00.
    """
    starts_at, minutes = utc(2026, 3, 8, 6, 30), 60
    (window,) = unavailable_windows(
        starts_at=starts_at, duration_minutes=minutes, timezone=NEW_YORK
    )

    # 01:30 EST through 03:30 EDT — two hours of wall clock for one hour of time.
    assert (window.start_time, window.end_time) == (dt.time(1, 30), dt.time(3, 30))
    assert resolved(window.date, window.start_time, NEW_YORK) == starts_at
    assert resolved(window.date, window.end_time, NEW_YORK) == starts_at + dt.timedelta(
        minutes=minutes
    )


def test_every_window_is_orderable_and_would_satisfy_the_check() -> None:
    """`exception_window_ordered` requires `end_time > start_time` on every row.

    Asserted here rather than only in the database, because the failure it
    guards is a mentor unable to cancel a session they are entitled to cancel.
    """
    for start, minutes, zone in [
        (utc(2026, 9, 1, 8, 0), 30, LAGOS),
        (utc(2026, 9, 1, 22, 30), 90, LAGOS),
        (utc(2026, 9, 1, 22, 0), 60, LAGOS),
        (utc(2026, 3, 8, 6, 30), 60, NEW_YORK),
        (utc(2026, 11, 1, 5, 0), 90, NEW_YORK),
    ]:
        for window in unavailable_windows(starts_at=start, duration_minutes=minutes, timezone=zone):
            assert window.end_time > window.start_time, (start, minutes, zone)


# --------------------------------------------------------------------------
# The invariant the whole design rests on
# --------------------------------------------------------------------------

#: The statuses a session can hold while its start is still ahead of it.
#:
#: `completed` and `no_show` are absent because neither can: both are written by
#: the attendance sweep after the session has run. That matters here — they
#: block `_busy` and are **not** in `LIVE_STATUSES`, which looks like the same
#: defect and is not, because `bookable` never offers a slot before `now`.
FUTURE_REACHABLE = frozenset(
    {
        SessionStatus.PENDING_MENTOR_APPROVAL,
        SessionStatus.CONFIRMED,
        SessionStatus.CANCELLED,
        SessionStatus.DECLINED,
        SessionStatus.WITHDRAWN,
        SessionStatus.EXPIRED,
    }
)


def test_nothing_hides_a_future_hour_the_constraint_would_not_protect() -> None:
    """**The pairing rule, asserted rather than remembered.**

    A status that `_busy` counts but `sessions_no_mentor_double_booking` ignores
    is an hour hidden from the grid and unguarded in the database — nobody can
    book it and nothing is holding it. That has been a live defect twice, and
    both times it was found by a person rather than by a gate.

    So: anything reachable on a *future* session that is not in
    `FREES_THE_HOUR` must be covered by `LIVE_STATUSES`. Adding a status to one
    list without the other fails here.
    """
    for status in FUTURE_REACHABLE:
        if status in FREES_THE_HOUR:
            continue
        assert f"'{status.value}'" in LIVE_STATUSES, (
            f"{status.value} blocks the grid but the exclusion constraint ignores it"
        )


def test_every_freed_status_is_one_the_constraint_also_ignores() -> None:
    """The other direction: a freed hour must not still be guarded.

    Less dangerous — it would refuse a booking the grid offered, which is a
    visible `409` rather than a silent disappearance — but it is the same list
    drifting, so it is pinned too.
    """
    for status in FREES_THE_HOUR:
        assert f"'{status}'" not in LIVE_STATUSES


# --------------------------------------------------------------------------
# The contract
# --------------------------------------------------------------------------


def test_only_cancelling_advertises_the_question() -> None:
    """**`accept` must not gain a field policy would have to ignore.**

    Its own docstring names that as the thing to avoid, which is why cancelling
    has its own model rather than a field on the one all four transitions share.
    """
    models = create_app(Settings(_env_file=None)).openapi()["components"]["schemas"]

    assert "release_slot" in models["SessionCancellationWrite"]["properties"]
    assert "release_slot" not in models["SessionTransitionWrite"]["properties"]
    # Still optional on all four. This change is not about reason codes.
    assert "required" not in models["SessionTransitionWrite"]


def test_cancelling_defaults_to_freeing_the_hour() -> None:
    """The default is the failure you can see.

    An hour offered while the mentor is busy arrives as a booking they can
    decline; an hour withheld while they are free arrives as nothing at all.
    """
    models = create_app(Settings(_env_file=None)).openapi()["components"]["schemas"]

    assert models["SessionCancellationWrite"]["properties"]["release_slot"]["default"] is True
