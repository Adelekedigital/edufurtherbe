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
    "UNDATED_REMINDER_DAYS",
    "cadence",
    "first_reminder_stage",
    "latest_due_stage",
    "reminder_due_at",
    "return_on_problem",
    "stage_after",
    "stage_before",
]

#: **A dated pause's reminders, in the order they fire**: days *before*
#: `return_on`. One template, sent at each (owner, 2026-10-01); the stage goes to
#: it as `daysUntilReturn`.
RETURN_REMINDER_OFFSETS: tuple[int, ...] = (7, 3, 0)

#: **An undated pause's nudges ("Not sure yet"), in the order they fire**: days
#: *after* the pause started — the legacy app's "Indefinite" pattern, nudged at
#: 30 and 59 days (owner, 2026-10-01). The same template, told `daysPaused`. Just
#: nudges: the pause itself has no cap and nothing resumes it.
UNDATED_REMINDER_DAYS: tuple[int, ...] = (30, 59)

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


def cadence(*, dated: bool) -> tuple[int, ...]:
    """The stages a pause runs through: before its date, or since it began."""
    return RETURN_REMINDER_OFFSETS if dated else UNDATED_REMINDER_DAYS


def reminder_due_at(offset: int, *, return_on: dt.date | None, paused_on: dt.date) -> dt.datetime:
    """When a stage goes out, as a local (naive) time in the mentor's zone.

    `RETURN_REMINDER_HOUR` on its day: `offset` days before `return_on` for a
    dated pause, `offset` days after `paused_on` (the local date the pause
    began) for an undated one. **The SQL claim states the same moment**
    (`mentor_listing.stage_due`), pinned to this by a boundary test.
    """
    if return_on is not None:
        day = return_on - dt.timedelta(days=offset)
    else:
        day = paused_on + dt.timedelta(days=offset)
    return dt.datetime.combine(day, dt.time(RETURN_REMINDER_HOUR))


def first_reminder_stage(
    *, return_on: dt.date | None, paused_on: dt.date, local_now: dt.datetime
) -> int | None:
    """The first stage still ahead when a pause is set or changed, or ``None``.

    **No late sends**: a stage whose moment has already passed is skipped, so
    a two-day pause gets only the day-of email rather than a "one week to go"
    that is already wrong.
    """
    for offset in cadence(dated=return_on is not None):
        if reminder_due_at(offset, return_on=return_on, paused_on=paused_on) > local_now:
            return offset
    return None


def latest_due_stage(
    *, return_on: dt.date | None, paused_on: dt.date, local_now: dt.datetime
) -> int | None:
    """The last stage whose moment has come, or ``None`` if none has.

    **After a gap only this one is sent**: a run that missed the week stage and
    finds the three-day stage due sends "3 days", never a late "7 days".
    """
    latest = None
    for offset in cadence(dated=return_on is not None):
        if reminder_due_at(offset, return_on=return_on, paused_on=paused_on) <= local_now:
            latest = offset
    return latest


def stage_after(offset: int, *, dated: bool) -> int | None:
    """The stage that follows this one, or ``None`` after the last."""
    stages = cadence(dated=dated)
    following = stages[stages.index(offset) + 1 : stages.index(offset) + 2]
    return following[0] if following else None


def stage_before(following: int | None, *, dated: bool) -> int:
    """The stage just sent, given the one now pending — the inverse of
    `stage_after`, which is how the claim recovers what it claimed."""
    stages = cadence(dated=dated)
    if following is None:
        return stages[-1]
    return stages[stages.index(following) - 1]
