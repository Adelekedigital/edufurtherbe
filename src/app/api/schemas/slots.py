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
