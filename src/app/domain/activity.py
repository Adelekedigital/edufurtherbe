"""Recording that somebody used the platform today (decision 342).

Pure: which day a request counts on, and the per-process memory that keeps
recording to one write per person per day. The write is `infra/db/user_activity`;
the counting rules are `analytics` (weekly and monthly active users).
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID

from app.domain.availability import local_date

#: The zone an activity day is a date in: one row a day whoever is looking.
ACTIVITY_TZ = "UTC"
#: How many people one process remembers for the day before starting over.
#: Past it a write is merely repeated, and the database ignores the repeat.
SEEN_TODAY_LIMIT = 50_000


def activity_day(moment: dt.datetime) -> dt.date:
    """The date ``moment`` falls on in `ACTIVITY_TZ`: an activity day."""
    return local_date(ACTIVITY_TZ, moment)


class SeenToday:
    """Who this process has already recorded on one day, at most ``limit``
    people: forgotten when the day changes or the set is full."""

    def __init__(self, limit: int = SEEN_TODAY_LIMIT) -> None:
        self._limit = limit
        self._day: dt.date | None = None
        self._seen: set[UUID] = set()

    def has(self, who: UUID, day: dt.date) -> bool:
        return day == self._day and who in self._seen

    def add(self, who: UUID, day: dt.date) -> None:
        if day != self._day or len(self._seen) >= self._limit:
            self._day, self._seen = day, set()
        self._seen.add(who)
