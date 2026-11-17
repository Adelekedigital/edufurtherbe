"""A mentor's own dashboard figures (decision 246), and their profile link's
visits and shares (decision 353).

**The window's bounds and blocks are required; every figure is optional.**
`tests/unit/test_added_fields_stay_optional.py` exists for fields added to an
object the frontend already builds in fixtures; it constrains what is
registered there, and these models are new. A figure may still be added to
later, so each has a default and is registered, the way a frontend-consumed
field would be. The bounds are never null, and a default would have to be a
made-up instant, so they are required like `MentorRelationshipRead`'s figures.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field

from app.api.schemas.common import COUNTED_MINUTES
from app.domain.enums import LinkVia, ShareMethod
from app.domain.link_tracking import TOP_REFERRERS
from app.domain.mentor_stats import DEFAULT_MONTHS, DEFAULT_WINDOW_DAYS


class WindowBoundsRead(BaseModel):
    """A dashboard window's edges, `[since, until)` in UTC: one definition for
    every dashboard (decisions 246, 249), so they cannot disagree about an edge."""

    since: dt.datetime = Field(description="Start of the window, UTC, **inclusive**.")
    until: dt.datetime = Field(description="End of the window, UTC, **exclusive**.")


class MentorStatsWindowRead(WindowBoundsRead):
    """One window's figures. Held and missed sessions belong to the window their
    `starts_at` falls in; a cancellation to the one it was made in; a review to
    the one it was written in."""

    completed_sessions: int = Field(
        default=0,
        description=(
            "Sessions held: `completed`, the same rule as `completed_sessions` on "
            "your public profile. This is the dashboard's *held* count."
        ),
    )
    mentoring_minutes: int = Field(
        default=0,
        description=f"Minutes of those sessions, as on your public profile. {COUNTED_MINUTES}",
    )
    mentees_mentored: int = Field(default=0, description="Distinct mentees across those sessions.")
    attendance_rate: int | None = Field(
        default=None,
        description=(
            "Your show-up rate, 0-100: settled sessions you attended over settled "
            "sessions you were expected at, as on your public profile. Null when "
            "no session in the window is settled."
        ),
    )
    missed_by_mentor: int = Field(
        default=0,
        description=(
            "Settled sessions in the window you were expected at and did not "
            "attend: the attendance rate's denominator less its numerator."
        ),
    )
    missed_by_mentee: int = Field(
        default=0,
        description=(
            "Settled sessions in the window your mentee was expected at and did "
            "not attend, read from the mentee's own attendance; a session you "
            "both missed counts here and in `missed_by_mentor`."
        ),
    )
    mentees_left_waiting: int = Field(
        default=0,
        description=(
            "Settled sessions in the window you did not attend while your mentee "
            "is recorded as attending: the part of `missed_by_mentor` where "
            "somebody was left waiting. A session you both missed, or one whose "
            "mentee attendance is unknown, is not counted."
        ),
    )
    cancelled_by_mentor: int = Field(
        default=0,
        description=(
            "Sessions you cancelled **during the window**, whenever they were "
            "due to start. Requests you declined are not cancellations, and "
            "neither are sessions anybody else ended."
        ),
    )
    review_count: int = Field(
        default=0, description="Published reviews of you written in the window."
    )
    average_rating: float | None = Field(
        default=None,
        description=(
            "The mean of those reviews' own values (each review's overall "
            "rating, or its older valuable rating), to two places. One formula "
            "in every window, so two windows compare. Not your profile's "
            "`session_value`, whose rule changes with how many reviews you "
            "have. Null with no reviews."
        ),
    )

    @classmethod
    def of(cls, row: dict[str, Any]) -> MentorStatsWindowRead:
        value = row["average_rating"]
        rate = row["attendance_rate"]
        return cls(
            since=row["since"],
            until=row["until"],
            completed_sessions=int(row["completed_sessions"]),
            mentoring_minutes=int(row["mentoring_minutes"]),
            mentees_mentored=int(row["mentees_mentored"]),
            attendance_rate=None if rate is None else int(rate),
            missed_by_mentor=int(row["missed_by_mentor"]),
            missed_by_mentee=int(row["missed_by_mentee"]),
            mentees_left_waiting=int(row["mentees_left_waiting"]),
            cancelled_by_mentor=int(row["cancelled_by_mentor"]),
            review_count=int(row["review_count"]),
            average_rating=None if value is None else float(value),
        )


class MentorStatsRead(BaseModel):
    """Your figures over a window and over the equal window before it, so a
    client computes the trend. Both blocks are read in one statement."""

    window_days: int = Field(
        default=DEFAULT_WINDOW_DAYS,
        description=(
            "The current window's length in days: the `window_days` asked for, "
            "or with `year` that year's length, 365 or 366."
        ),
    )
    current_streak: int | None = Field(
        default=None,
        description=(
            "Sessions you have held in a row since you last missed one or "
            "cancelled one yourself (a cancellation counts from when you made "
            "it), all time rather than in a window; nothing else (a cancellation "
            "by anyone else, a mentee's no-show, a session not yet held) adds "
            "to it or resets it, and it is 0 when you have held none since."
        ),
    )
    current: MentorStatsWindowRead = Field(description="The window ending now.")
    previous: MentorStatsWindowRead = Field(
        description="The equal window immediately before `current`."
    )


class MentorMonthRead(BaseModel):
    """One UTC calendar month of the chart (decision 247): the same figures as
    a dashboard window, with the month as the window."""

    month: str = Field(description="The month, `YYYY-MM`, UTC.", examples=["2026-10"])
    since: dt.datetime = Field(description="Start of the month, UTC, **inclusive**.")
    until: dt.datetime = Field(description="Start of the next month, UTC, **exclusive**.")
    completed_sessions: int = Field(
        default=0,
        description=(
            "Sessions held: `completed`, starting in the month. The same rule as "
            "`completed_sessions` in `/me/mentor-stats`."
        ),
    )
    missed_by_mentor: int = Field(
        default=0,
        description=(
            "Settled sessions starting in the month you were expected at and did "
            "not attend, as in `/me/mentor-stats`."
        ),
    )
    cancelled_by_mentor: int = Field(
        default=0,
        description="Sessions you cancelled **during the month**, whenever they were due.",
    )
    cancelled_by_mentee: int = Field(
        default=0,
        description="Sessions of yours your mentee cancelled during the month.",
    )
    cancelled_by_other: int = Field(
        default=0,
        description=(
            "Sessions of yours cancelled during the month by anybody else: the "
            "platform, an admin, or nobody recorded."
        ),
    )

    @classmethod
    def of(cls, row: dict[str, Any]) -> MentorMonthRead:
        return cls(
            month=row["month"],
            since=row["since"],
            until=row["until"],
            completed_sessions=int(row["completed_sessions"]),
            missed_by_mentor=int(row["missed_by_mentor"]),
            cancelled_by_mentor=int(row["cancelled_by_mentor"]),
            cancelled_by_mentee=int(row["cancelled_by_mentee"]),
            cancelled_by_other=int(row["cancelled_by_other"]),
        )


class MentorMonthlyStatsRead(BaseModel):
    """Your figures per UTC calendar month, oldest first, every month present."""

    months: int = Field(default=DEFAULT_MONTHS, description="How many months `series` holds.")
    series: list[MentorMonthRead] = Field(
        description=(
            "One entry per month, oldest first, ending with this month; a month "
            "with nothing in it is zeros, never missing."
        )
    )


class LinkViaCountRead(BaseModel):
    """Visits that arrived through one channel."""

    key: LinkVia | None = Field(
        description=(
            "The `?via=` the link carried, as recorded by "
            "`POST /mentors/{handle}/visits`: `copy`, `share` or `invite`. Null "
            "for a visit whose link carried none of ours, such as a link typed, "
            "edited or found by search."
        )
    )
    count: int = Field(description="Visits, one per visitor per UTC day.")


class ReferrerCountRead(BaseModel):
    """Visits from one site."""

    host: str = Field(description="The site's bare host name.", examples=["wa.me"])
    count: int = Field(description="Visits from it, one per visitor per UTC day.")


class ShareMethodCountRead(BaseModel):
    """Shares one way."""

    key: ShareMethod = Field(
        description="`copy` (the link was copied) or `native` (the device's share sheet)."
    )
    count: int = Field(description="Shares, one per person per method per UTC day.")


class MentorLinkStatsWindowRead(BaseModel):
    """One window's profile link figures, by UTC day: a visit or share belongs
    to the window its day is in. Counts only, never anything about a visitor."""

    since: dt.date = Field(description="The window's first UTC day, **inclusive**.")
    until: dt.date = Field(
        description="The UTC day after its last, **exclusive**: the next window's `since`."
    )
    visits: int = Field(
        default=0,
        description=(
            "Visits to your public profile: each visitor counts once a UTC day, so "
            "this is also the sum of each day's unique visitors. Somebody who comes "
            "back on another day counts again, because visitors are never matched "
            "across days. Your own visits while signed in are never recorded, and "
            "nothing was recorded before link tracking went live, so a window "
            "before then reads zero."
        ),
    )
    visits_by_via: list[LinkViaCountRead] = Field(
        default_factory=list,
        description=(
            "`visits` by the channel the link came through: every channel, "
            "`copy`, `share`, `invite`, then null, zeros included, in that order. "
            "The counts add up to `visits`."
        ),
    )
    top_referrers: list[ReferrerCountRead] = Field(
        default_factory=list,
        description=(
            f"The sites visitors came from, at most {TOP_REFERRERS}, most visits "
            "first and a tie by host name. A visit the browser named no site for "
            "is in `visits` and in no host here, so these need not add up to it."
        ),
    )
    shares: int = Field(
        default=0,
        description=(
            "Times your profile link was shared: each person "
            "counts once per method per UTC day. Shares you made yourself while "
            "signed in are left out."
        ),
    )
    shares_by_method: list[ShareMethodCountRead] = Field(
        default_factory=list,
        description=(
            "`shares` by method: every method, `copy` then `native`, zeros "
            "included. The counts add up to `shares`."
        ),
    )


class MentorLinkStatsRead(BaseModel):
    """Your profile link's figures over a window and the equal window before it,
    both read in one statement."""

    window_days: int = Field(
        default=DEFAULT_WINDOW_DAYS,
        description=(
            "How many UTC days `current` holds: the `window_days` asked for, or "
            "with `year` that year's 365 or 366."
        ),
    )
    current: MentorLinkStatsWindowRead = Field(
        description="The `window_days` days ending with today, today included; or the `year`."
    )
    previous: MentorLinkStatsWindowRead = Field(
        description=("The equal window immediately before `current`; with `year`, the year before.")
    )
