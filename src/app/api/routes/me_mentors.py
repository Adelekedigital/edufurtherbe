"""The mentors the caller has met (`/me/mentors`, decision 248)."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.api.deps import PRIVATE_NO_STORE, MetMentorsDep
from app.api.schemas.common import Page
from app.api.schemas.met_mentors import MetMentorRead

router = APIRouter(prefix="/api/v1/me/mentors", tags=["my-mentors"])

RESPONSES: dict[int | str, dict[str, str]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "The bearer token is absent, malformed, expired or wrongly signed."
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "description": "`cursor` is not one this endpoint issued, or `limit` is out of range."
    },
}


@router.get(
    "",
    response_model=Page[MetMentorRead],
    summary="Mentors you have met",
    description=(
        "Each mentor you have had at least one **completed** session with as a "
        "mentee, most recent session first: how many, when the last one "
        "started, and what you can do next with them (`bookability`).\n\n"
        "**A mentor who deleted their account or mentor profile is not "
        "listed**, though your "
        "sessions with them stay in your session history. Cancelled, declined, "
        "expired and missed sessions do not count as meeting someone.\n\n"
        "Only ever your own sessions. **If you have met nobody, or are not a "
        "mentee, you get an empty page**, not an error. Never cached by anyone "
        "but you."
    ),
    responses=RESPONSES,
)
async def list_mentors_met(page: MetMentorsDep, response: Response) -> Page[MetMentorRead]:
    response.headers["Cache-Control"] = PRIVATE_NO_STORE
    rows, cursor = page
    return Page(data=[MetMentorRead.of(row) for row in rows], next_cursor=cursor)
