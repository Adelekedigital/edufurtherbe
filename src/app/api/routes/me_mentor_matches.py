"""A signed-in mentee's top mentor matches (`/me/mentor-matches`, decision 265)."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.api.deps import PRIVATE_NO_STORE, MentorMatchesDep
from app.api.schemas.matches import MentorMatchesRead

router = APIRouter(prefix="/api/v1/me/mentor-matches", tags=["mentor-matches"])


@router.get(
    "",
    response_model=MentorMatchesRead,
    summary="Your top mentor matches",
    description=(
        "The mentors who suit your onboarding answers best, best first, each with "
        "the reasons it matched. Only mentors you can book now are suggested, "
        "never yourself.\n\n"
        "**Ranked by**, most important first, each beating everything below it: "
        "how many of your help areas the mentor offers; your field of study "
        "against what they studied, matched as Explore search matches it (every "
        "word, or a near spelling of every long word; case- and accent-"
        "insensitive); being from your country; having any free time (sooner is not "
        "better); studying in one of your destinations, first choice first; and "
        "holding your goal degree level or higher. Ties are shuffled once per "
        "day for you, as on Explore. An answer you have not given scores nothing.\n\n"
        "**`mentor`** is exactly one `GET /mentors` item. **`reasons`** lists "
        "only the signals that matched, in that order. **`based_on`** is your "
        "own answers, and nothing here describes any other mentee.\n\n"
        "**`for_mentor`** (an id or slug) asks about one mentor: if you can book "
        "them, `matches` holds only them and `for_mentor_bookable` is `true`; if "
        "not (paused, unlisted, no live session type, unknown), it is `false` "
        "and `matches` is your usual list without them.\n\n"
        "`matches` is empty only when nobody can be booked. Never cached."
    ),
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "description": "The bearer token is absent, malformed, expired or wrongly signed."
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": "`limit` is outside 1 to 10, or `for_mentor` is over 128 characters."
        },
    },
)
async def my_mentor_matches(matches: MentorMatchesDep, response: Response) -> MentorMatchesRead:
    response.headers["Cache-Control"] = PRIVATE_NO_STORE
    return matches
