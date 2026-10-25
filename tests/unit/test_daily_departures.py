"""A `participant.left` delivery, and the join id both events carry (#394).

Shapes from Daily's own reference (docs.daily.co, *participant.left* and
*participant.joined*): `session_id` is unique to a single join and present on
both, and a leave carries its join's `joined_at` plus `duration` in seconds.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any

import pytest

from app.domain.session_minutes import LONGEST_STAY
from app.infra.clients.daily_presence import (
    STAY_ID_MAX_LENGTH,
    Departure,
    Sighting,
    _moment,
    departure_from,
    sighting_from,
)

ROOM = "ef-daily-0193a5b2-0000-7000-8000-000000000001"
USER = "0193a5b2-0000-7000-8000-0000000000aa"
JOINED = 1708972291.567


def leave(**payload: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "room": ROOM,
        "user_id": USER,
        "session_id": "0c0d2dda-f21d-4cf9-ab56-86bf3c407ffa",
        "joined_at": JOINED,
        "duration": 11.419000148773193,
    }
    body.update(payload)
    return {"version": "1.0.0", "type": "participant.left", "payload": body}


def test_a_participant_left_is_the_join_it_ends_and_when() -> None:
    joined_at = dt.datetime.fromtimestamp(JOINED, tz=dt.UTC)

    assert departure_from(leave()) == Departure(
        sighting=Sighting(ROOM, USER, joined_at, stay="0c0d2dda-f21d-4cf9-ab56-86bf3c407ffa"),
        left=joined_at + dt.timedelta(seconds=11.419000148773193),
    )


def test_the_longest_real_stay_is_a_departure() -> None:
    """The bound is the records' own (decision 281), and inclusive."""
    found = departure_from(leave(duration=LONGEST_STAY.total_seconds(), joined_at=0))

    assert found is not None
    assert found.left - found.sighting.at == LONGEST_STAY


def test_a_leave_of_no_length_is_a_departure() -> None:
    found = departure_from(leave(duration=0))

    assert found is not None
    assert found.left == found.sighting.at


@pytest.mark.parametrize(
    "duration",
    [
        pytest.param(None, id="missing"),
        pytest.param("12", id="a string"),
        pytest.param(True, id="a boolean"),
        pytest.param(-1, id="negative"),
        pytest.param(math.nan, id="NaN"),
        pytest.param(math.inf, id="infinite"),
        pytest.param(LONGEST_STAY.total_seconds() + 1, id="longer than any real stay"),
    ],
)
def test_a_leave_without_a_usable_duration_ends_at_its_join(duration: object) -> None:
    """**The event says they left**, so the stay still closes, at its join,
    and the join is still sighted; only the length is unknown. Never a crash."""
    payload = leave(duration=duration)
    if duration is None:
        del payload["payload"]["duration"]

    found = departure_from(payload)

    assert found is not None
    assert found.left == found.sighting.at == dt.datetime.fromtimestamp(JOINED, tz=dt.UTC)


@pytest.mark.parametrize(
    "event",
    [
        {"test": "test"},
        {"type": "participant.joined", "payload": leave()["payload"]},
        {"type": "participant.left", "payload": "not an object"},
        leave(room=None),
        leave(user_id=""),
        leave(joined_at=1e300),
    ],
)
def test_anything_but_a_complete_leave_is_no_departure(event: dict[str, Any]) -> None:
    assert departure_from(event) is None


def test_a_join_is_never_a_departure_and_a_leave_never_a_join() -> None:
    assert sighting_from(leave()) is None


@pytest.mark.parametrize(
    ("stay", "kept"),
    [
        pytest.param("x", "x", id="one character"),
        pytest.param("s" * STAY_ID_MAX_LENGTH, "s" * STAY_ID_MAX_LENGTH, id="the longest stored"),
        pytest.param("s" * (STAY_ID_MAX_LENGTH + 1), None, id="too long to store"),
        pytest.param("", None, id="empty"),
        pytest.param(12345, None, id="not a string"),
    ],
)
def test_a_join_id_is_kept_only_when_it_can_be_stored(stay: object, kept: str | None) -> None:
    """**The sighting survives either way**: attendance must not depend on an
    id we only need for *in the room now*."""
    found = departure_from(leave(session_id=stay))

    assert found is not None
    assert found.sighting.stay == kept


def test_the_stored_limit_is_the_column_s() -> None:
    """Pinned to the `CHECK` on `session_room_stays.provider_session_id`, so a
    longer id becomes "no stay" rather than a refused insert and a 500."""
    from app.infra.db.models.sessions import SessionRoomStay

    checks = [
        str(c.sqltext)  # type: ignore[attr-defined]
        for c in SessionRoomStay.__table__.constraints
        if getattr(c, "name", "") and "provider_session_id_length" in str(c.name)
    ]

    assert checks == [f"char_length(provider_session_id) BETWEEN 1 AND {STAY_ID_MAX_LENGTH}"]


def test_a_leave_whose_end_no_clock_can_hold_ends_at_its_join() -> None:
    """A join at the very end of the calendar plus a real length overflows a
    `datetime`; the leave still ends the stay, at its join, never a crash."""
    last_second = dt.datetime(9999, 12, 31, 23, 59, 59, tzinfo=dt.UTC).timestamp()
    if _moment(last_second) is None:
        # Windows' clock stops in year 3000, so the join itself is unreadable
        # there; CI (Linux) reaches the overflow.
        pytest.skip("this platform cannot hold the join's instant")

    found = departure_from(leave(joined_at=last_second, duration=3600))

    assert found is not None
    assert found.left == found.sighting.at
