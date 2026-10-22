"""The card's three states, as SQL: one `CASE` for a mentor and for each offering.

`open`, `none` or `refreshing`, over a row the refresh job wrote
(`next_available_store`, ADR 0029): `mentor_next_availability` for the mentor,
`session_type_next_availability` for one of their offerings (decision 284).
Both are vouched for by the same change log, so **one rule decides both** and
an offering can never read `open` where its mentor's log says a change is
unseen.

Apart from the job so `session_type_store`, which the job reads offerings
through, can carry an offering's state in its own statement.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, case, exists, func, literal, select
from sqlalchemy.orm import aliased

from app.domain.availability import BookingWindow
from app.infra.db.booking_rules import effective_window_days
from app.infra.db.models.availability import (
    MentorAvailabilityChange,
    MentorNextAvailability,
    SessionTypeNextAvailability,
)
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.sessions import SessionTypeBookingConfig
from app.infra.db.public_visibility import taking_bookings

__all__ = [
    "NONE",
    "OPEN",
    "REFRESHING",
    "free_within",
    "next_available_state",
    "public_next_available_state",
    "public_session_type_next_available_state",
    "unchanged",
    "vouched",
]

OPEN = "open"
NONE = "none"
REFRESHING = "refreshing"

#: Either cached row: both carry these two columns and a mentor.
_Cached = type[MentorNextAvailability] | type[SessionTypeNextAvailability]


def unchanged() -> Any:
    """A value was computed and nothing has changed for this mentor since."""
    changed = exists().where(
        MentorAvailabilityChange.mentor_user_id == MentorNextAvailability.mentor_user_id
    )
    return MentorNextAvailability.id.is_not(None) & ~changed


def _window_days(window: BookingWindow, row: _Cached, session_type_id: Any) -> Any:
    """The cached offering's effective window **now** — `effective_window_days`
    over its own config and its mentor's default, under the current settings."""
    config = aliased(SessionTypeBookingConfig)
    return (
        select(effective_window_days(window, config, MentorProfile))
        .where(
            config.session_type_id == session_type_id,
            MentorProfile.user_id == row.mentor_user_id,
        )
        .correlate(row)
        .scalar_subquery()
    )


def _state(window: BookingWindow, row: _Cached, session_type_id: Any, vouchable: Any) -> Any:
    """The `CASE`, over one cached row.

    **Probes the change log once per row.** The time itself is shown only when
    this says `open`; the schema applies that, so it is never evaluated twice.

    A slot whose booking window has closed reads as `refreshing`, not `none`:
    something *was* free, and the next run will say what is free now.

    **So does a cached time past the cached offering's window as it is now**
    (#221). The cache was vouched for under the settings it was computed with;
    lower the maximum or the platform default and `/slots` and booking refuse
    a time the card would still offer. Compared through `effective_window_days`,
    the one resolution, so an offering with its own longer window keeps its time.
    """
    return case(
        (~vouchable, REFRESHING),
        (
            row.next_available_at
            > func.now() + func.make_interval(0, 0, 0, _window_days(window, row, session_type_id)),
            REFRESHING,
        ),
        (row.bookable_until > func.now(), OPEN),
        (row.next_available_at.is_(None), NONE),
        else_=REFRESHING,
    )


def next_available_state(window: BookingWindow) -> Any:
    """The mentor's state, over `mentor_next_availability`."""
    row = MentorNextAvailability
    return _state(window, row, row.next_available_session_type_id, unchanged())


def _public(state: Any) -> Any:
    """`state`, but `none` for anyone a stranger cannot book.

    **The job refreshes `bookable_mentors()` and nobody else**, so a visible
    mentor who is not taking bookings — or an owner reading a hidden profile —
    has either no row, which reads `refreshing` and promises a time that never
    comes, or a left-over one the job has stopped watching. Both read `none`.
    Gated on `taking_bookings()`, the same expression the flag beside it reads
    and the job's `bookable_mentors()` filters on, so all three agree.
    """
    return case((taking_bookings(), state), else_=literal(NONE))


def public_next_available_state(window: BookingWindow) -> Any:
    """The mentor's state as a stranger reads it."""
    return _public(next_available_state(window))


def public_session_type_next_available_state(window: BookingWindow) -> Any:
    """One offering's state as a stranger reads it (decision 284).

    Needs both cached rows joined: the offering's own, and its mentor's, whose
    change log vouches for it. No row for the offering — one added since the
    refresh, or any before the first refresh after deploy — reads `refreshing`.
    """
    row = SessionTypeNextAvailability
    return _public(_state(window, row, row.session_type_id, unchanged() & row.id.is_not(None)))


def vouched(window: BookingWindow) -> Any:
    """The card vouches for the stored time: its public state is `open`.

    **The one spelling of the gate as a predicate**, for a query that filters or
    sorts on it rather than reading the state as a column. The card's schema
    shows the time only when that column says `open`, so the two agree by
    construction.
    """
    return public_next_available_state(window) == OPEN


def free_within(window: BookingWindow, days: int) -> Any:
    """`vouched()`, and the stored time is before `now() + days x 24 hours`.

    Half-open, by the database's `now()` (the transaction's start), the clock
    `_state()` compares `bookable_until` against. Hours rather than days, so
    the edge is exact whatever the connection's `TimeZone`: a `timestamptz`
    plus `'1 day'` steps calendar days in that zone.
    """
    return and_(
        vouched(window),
        MentorNextAvailability.next_available_at
        < func.now() + func.make_interval(0, 0, 0, 0, days * 24),
    )
