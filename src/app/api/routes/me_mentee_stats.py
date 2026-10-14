"""A mentee's own dashboard home figures (`/me/mentee-stats`, decision 249)."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.api.deps import PRIVATE_NO_STORE, OwnMenteeStatsDep
from app.api.schemas.mentee_stats import MenteeStatsRead

router = APIRouter(prefix="/api/v1/me/mentee-stats", tags=["mentee-stats"])

RESPONSES: dict[int | str, dict[str, str]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "The bearer token is absent, malformed, expired or wrongly signed."
    },
}


@router.get(
    "",
    response_model=MenteeStatsRead,
    summary="Your figures as a mentee",
    description=(
        "What your dashboard home shows: sessions held, mentors met and minutes "
        "of guidance all time, sessions you missed in the last "
        "`missed_window_days` days, and sessions and minutes for the last "
        "`window_days` days (`current`) beside the equal window before "
        "(`previous`). Raw numbers: compute the trend from the two.\n\n"
        "Windows are UTC and half-open: `since` is inside, `until` is not. A "
        "session belongs to the window its `starts_at` falls in. Everything is "
        "read at one instant.\n\n"
        "Only ever sessions you received as a mentee, never ones you gave as a "
        "mentor. **With none you get zeros**, not an error. Never cached by "
        "anyone but you."
    ),
    responses=RESPONSES,
)
async def read_own_mentee_stats(figures: OwnMenteeStatsDep, response: Response) -> MenteeStatsRead:
    response.headers["Cache-Control"] = PRIVATE_NO_STORE
    return MenteeStatsRead.of(figures)
