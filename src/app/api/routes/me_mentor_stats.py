"""A mentor's own dashboard figures (`/me/mentor-stats`, decision 246)."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.api.deps import OwnMentorStatsDep
from app.api.schemas.mentor_stats import MentorStatsRead, MentorStatsWindowRead

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
        "Windows are UTC and half-open: `since` is inside, `until` is not. A "
        "session belongs to the window its `starts_at` falls in; a review to "
        "the one it was written in. Each figure follows the same rule as on "
        "your public profile.\n\n"
        "Only ever your own sessions. **If you are not a mentor you get zeros "
        "and nulls**, not an error. Never cached by anyone but you."
    ),
    responses=RESPONSES,
)
async def read_own_mentor_stats(stats: OwnMentorStatsDep, response: Response) -> MentorStatsRead:
    response.headers["Cache-Control"] = "private, no-store"
    return MentorStatsRead(
        window_days=stats["window_days"],
        current=MentorStatsWindowRead.of(stats["current"]),
        previous=MentorStatsWindowRead.of(stats["previous"]),
    )
