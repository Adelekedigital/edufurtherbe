"""Rules about a mentor taking themselves off the listing and coming back.

Pure: the caller supplies the mentor's own today, because "today" for a return
date is the date in *their* zone, which only the data layer can know.
"""

from __future__ import annotations

import datetime as dt

__all__ = [
    "RETURN_ON_POINTER",
    "RETURN_REMINDER_HOUR",
    "RETURN_REMINDER_OFFSETS",
    "first_reminder_stage",
    "reminder_due_at",
    "return_on_problem",
    "stage_after",
    "stage_before",
]

#: **The return reminder's cadence, in the order it fires**: days before
#: `return_on`. One template, sent at each (owner, 2026-10-01); the stage goes to
#: it as `daysUntilReturn`. The one place the stages are written — their order
#: and the claim's step from one to the next both come from it.
RETURN_REMINDER_OFFSETS: tuple[int, ...] = (7, 3, 0)

#: The local hour every stage goes out at, on its day.
RETURN_REMINDER_HOUR = 8

#: Where a refused return date is reported in the pause request.
RETURN_ON_POINTER = "/return_on"


def return_on_problem(return_on: dt.date | None, today: dt.date) -> str | None:
    """Why this return date cannot be set, or ``None`` if it can.

    **After the mentor's today, never on it.** The reminder goes out on the
    morning of the date, so a date that is today or past would be due at once:
    a pause that reminds the mentor to come back before they have left. Null is
    "not sure yet" and is always allowed.
    """
    if return_on is not None and return_on <= today:
        return "return_on must be after today in your time zone"
    return None


def reminder_due_at(return_on: dt.date, offset: int) -> dt.datetime:
    """When the stage `offset` days before `return_on` goes out, as a local
    (naive) time in the mentor's zone."""
    day = return_on - dt.timedelta(days=offset)
    return dt.datetime.combine(day, dt.time(RETURN_REMINDER_HOUR))


def first_reminder_stage(return_on: dt.date, local_now: dt.datetime) -> int | None:
    """The first stage still ahead when a pause sets this date, or ``None``.

    **No late sends**: a stage whose moment has already passed when the pause
    is set or changed is skipped, so a two-day pause gets only the day-of
    email rather than a "one week to go" that is already wrong.
    """
    for offset in RETURN_REMINDER_OFFSETS:
        if reminder_due_at(return_on, offset) > local_now:
            return offset
    return None


def stage_after(offset: int) -> int | None:
    """The stage that follows this one, or ``None`` after the last."""
    index = RETURN_REMINDER_OFFSETS.index(offset)
    following = RETURN_REMINDER_OFFSETS[index + 1 : index + 2]
    return following[0] if following else None


def stage_before(following: int | None) -> int:
    """The stage just sent, given the one now pending — the inverse of
    `stage_after`, which is how the claim recovers what it claimed."""
    if following is None:
        return RETURN_REMINDER_OFFSETS[-1]
    return RETURN_REMINDER_OFFSETS[RETURN_REMINDER_OFFSETS.index(following) - 1]
