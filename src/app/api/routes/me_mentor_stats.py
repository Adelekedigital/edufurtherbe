"""A mentor's own dashboard figures (`/me/mentor-stats`, decision 246), their
monthly chart (`/me/mentor-stats/monthly`, decision 247) and their profile
link's visits and shares (`/me/mentor-stats/profile-link`, decision 353)."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.api.deps import (
    PRIVATE_NO_STORE,
    OwnLinkStatsDep,
    OwnMentorMonthlyStatsDep,
    OwnMentorStatsDep,
)
from app.api.schemas.mentor_stats import (
    MentorLinkStatsRead,
    MentorLinkStatsWindowRead,
    MentorMonthlyStatsRead,
    MentorMonthRead,
    MentorStatsRead,
    MentorStatsWindowRead,
)

router = APIRouter(prefix="/api/v1/me/mentor-stats", tags=["mentor-stats"])

RESPONSES: dict[int | str, dict[str, str]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "The bearer token is absent, malformed, expired or wrongly signed."
    },
}


@router.get(
    "",
    response_model=MentorStatsRead,
    summary="Your figures over a window, and the window before",
    description=(
        "What your dashboard shows: sessions held, minutes, mentees, show-up "
        "rate, sessions you missed or cancelled, and your reviews, for the "
        "`window_days` ending now (`current`) and the same length before it "
        "(`previous`). Compute the trend from the two.\n\n"
        "Or pass `year` instead: `current` is that UTC calendar year, running "
        "to 31 December even while it is under way, and `previous` the "
        "calendar year before, which in a leap year is a day longer or "
        "shorter. `window_days` in the response is then that year's "
        "length.\n\n"
        "Windows are UTC and half-open: `since` is inside, `until` is not. A "
        "held or missed session belongs to the window its `starts_at` falls "
        "in; a cancellation to the one it was made in; a review to the one it "
        "was written in. Sessions, minutes, mentees and show-up rate follow "
        "the same rules as on your public profile. Both windows are read at "
        "one instant.\n\n"
        "`current_streak` is not windowed: the sessions you have held in a "
        "row since you last missed one or cancelled one yourself, read at the "
        "same instant.\n\n"
        "Only ever your own sessions. **If you are not a mentor you get zeros "
        "and nulls**, not an error. Never cached by anyone but you."
    ),
    responses=RESPONSES,
)
async def read_own_mentor_stats(stats: OwnMentorStatsDep, response: Response) -> MentorStatsRead:
    response.headers["Cache-Control"] = PRIVATE_NO_STORE
    return MentorStatsRead(
        window_days=stats["window_days"],
        current_streak=int(stats["current_streak"]),
        current=MentorStatsWindowRead.of(stats["current"]),
        previous=MentorStatsWindowRead.of(stats["previous"]),
    )


@router.get(
    "/monthly",
    response_model=MentorMonthlyStatsRead,
    summary="Your sessions per month, for a chart",
    description=(
        "Per UTC calendar month, for the last `months` months ending with this "
        "one, oldest first: sessions held, sessions you missed, and "
        "cancellations split by who made them (you, your mentee, or anybody "
        "else). **Every month is present**, with zeros when nothing happened, "
        "so the chart has no gaps.\n\n"
        "The same rules as `/me/mentor-stats`, with each month as the window: "
        "a held or missed session belongs to the month its `starts_at` falls "
        "in; a cancellation to the month it was made in. Months are half-open: "
        "`since` is inside, `until` is not. All months are read at one "
        "instant.\n\n"
        "Only ever your own sessions. **If you are not a mentor you get zeros**, "
        "not an error. Never cached by anyone but you."
    ),
    responses=RESPONSES,
)
async def read_own_mentor_monthly_stats(
    stats: OwnMentorMonthlyStatsDep, response: Response
) -> MentorMonthlyStatsRead:
    response.headers["Cache-Control"] = PRIVATE_NO_STORE
    return MentorMonthlyStatsRead(
        months=stats["months"], series=[MentorMonthRead.of(row) for row in stats["series"]]
    )


@router.get(
    "/profile-link",
    response_model=MentorLinkStatsRead,
    summary="Your profile link's visits and shares, by channel",
    description=(
        "How people reached your public profile and passed it on, for the "
        "`window_days` UTC days ending with today (`current`, today included) "
        "and the same number of days before it (`previous`); or, with `year`, "
        "that UTC calendar year and the one before. The same window parameters "
        "as `/me/mentor-stats`.\n\n"
        "Visits and shares are recorded by UTC day, so a window is whole days: "
        "`since` is its first, `until` the day after its last. A visitor counts "
        "once a day, and is never matched across days, so `visits` is also the "
        "sum of each day's unique visitors. `visits_by_via` splits it by the "
        "channel the link came through, `top_referrers` names the sites "
        "visitors came from, and `shares_by_method` splits `shares` by how the "
        "link was shared. Shares you made yourself while signed in are left "
        "out.\n\n"
        "**Counts only**: nothing about any one visitor is ever returned. Only "
        "ever your own profile. **If nobody has visited or shared it, or you "
        "are not a mentor, you get zeros**, not an error. Never cached by "
        "anyone but you."
    ),
    responses=RESPONSES,
)
async def read_own_link_stats(stats: OwnLinkStatsDep, response: Response) -> MentorLinkStatsRead:
    response.headers["Cache-Control"] = PRIVATE_NO_STORE
    return MentorLinkStatsRead(
        window_days=stats["window_days"],
        current=MentorLinkStatsWindowRead(**stats["current"]),
        previous=MentorLinkStatsWindowRead(**stats["previous"]),
    )
