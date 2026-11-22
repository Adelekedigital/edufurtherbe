"""What a mentor's free time looks like to somebody deciding whether to book.

**Instants, never a local string.** A slot goes out as UTC and the browser
renders it in whoever's zone is looking. A session between Lagos and Toronto has
no single correct local time, and a server-formatted one is what
``12hr-localStartTime-TXT`` was in the legacy app — where it disagreed with the
stored time by five hours on half the rows.

**Both ends, though one is derivable.** ``end`` is always ``start`` plus the
session type's duration, and it is returned anyway: a client that computes it
has a second copy of a rule that lives in ``session_type_booking_configs``, and
the copy is the one that will be wrong when a mentor changes their offering.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field

from app.api.schemas.common import Page
from app.domain.availability import UtcInterval


class SlotRead(BaseModel):
    """One bookable span of a mentor's time.

    A slot appearing here is not a reservation. Two people can be looking at the
    same one, and the booking that arrives second is refused by
    `sessions_no_mentor_double_booking` — the constraint, not a check this
    endpoint could have made, because anything it decided would already be stale
    by the time the answer reached a browser.
    """

    start: dt.datetime = Field(
        description="UTC instant the session would begin. Render it client-side."
    )
    end: dt.datetime = Field(
        description=(
            "UTC instant it would end — `start` plus the session type's "
            "`duration_minutes`, returned rather than left for the client to "
            "recompute from a duration it would have to fetch separately."
        )
    )

    @classmethod
    def from_interval(cls, interval: UtcInterval) -> SlotRead:
        return cls(start=interval.start, end=interval.end)


class SlotsPage(Page[SlotRead]):
    """A page of slots, and the next time after a full window (owner, 2026-11-21).

    Both extra fields are set only when nothing is free anywhere inside the
    offering's booking window, whatever range was asked for, and both are null
    together. They are named, never offered: the window is a hard limit, so the
    time never appears in `data` and cannot be booked until `bookable_from`.
    """

    next_after_window_at: dt.datetime | None = Field(
        default=None,
        description=(
            "UTC instant of this offering's first free start after its booking "
            "window closes. Set only when nothing is free anywhere inside the "
            "window; null when the window has a free time, when nothing is "
            "free within 28 days after it, or when the mentor's calendar cannot be "
            "read just then (a time is never named that it might have busy). The "
            "same notice, breaks, sessions, holds, blocks and calendar apply as to "
            "`data`."
        ),
    )
    bookable_from: dt.datetime | None = Field(
        default=None,
        description=(
            "UTC instant `next_after_window_at` becomes bookable: that start "
            "minus the offering's booking window. Render it in the viewer's zone "
            '("you can book it from Sun, Nov 22"). Null exactly when '
            "`next_after_window_at` is."
        ),
    )
