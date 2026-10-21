"""How many minutes an EduFurther video session counts (Integrations PRD, 2026-10-21).

**The longer of measured and booked.** Measured is the time the mentor and the
mentee were in the room *together*, from each person's stays in Daily's meeting
records; one person waiting alone adds nothing. Booked stays the floor, so a
call that ended early never counts less than the session it was. Google Meet
reports no stays, and migrated sessions have none, so both count booked.

This module holds the arithmetic; the store picks the sessions (in SQL, from
``PRESENCE_RECORDS_LAG`` and ``PRESENCE_RECORDS_PATIENCE``) and the larger
number, where the stats are summed.

**Read only once the call is over.** Daily's records are readable during a call
with partial durations (`docs/daily-spike-guide.md`), and the room admits no new
join after the session's end but lets those inside stay, so a record is final
only when no meeting in the room is still going. The reader checks that.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

__all__ = ["Interval", "co_present_minutes"]

#: One person in the room, from one instant to another.
Interval = tuple[dt.datetime, dt.datetime]


def _joined(stays: Iterable[Interval]) -> list[Interval]:
    """One person's stays as disjoint intervals. Daily writes a record per join,
    and a rejoin can overlap the stay it replaced, so they are merged first."""
    merged: list[Interval] = []
    for start, end in sorted(stays):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def co_present_minutes(mentor: Iterable[Interval], mentee: Iterable[Interval]) -> int:
    """Whole minutes both were in the room at once, rounded down."""
    together = dt.timedelta()
    for a_start, a_end in _joined(mentor):
        for b_start, b_end in _joined(mentee):
            overlap = min(a_end, b_end) - max(a_start, b_start)
            if overlap > dt.timedelta():
                together += overlap
    return int(together.total_seconds() // 60)
