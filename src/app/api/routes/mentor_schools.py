"""Where the mentors you can book studied — a social-proof strip (decision 307).

**Its own path, not `/mentors/schools`**, for the reason `featured.py` gives:
mentor slugs are any `[a-z0-9-]+` and nothing reserves a word, so a literal
under `/mentors/` would shadow a real mentor's profile. It also keeps the strip
out of `PerViewerHeadersMiddleware`'s `/mentors` root: the answer is the same
for every viewer, so a signed-in visitor still reads it from a shared cache.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.api.deps import MentorSchoolsDep
from app.api.deps.mentors import DEFAULT_SCHOOLS, MAX_SCHOOLS
from app.api.schemas.common import Page
from app.api.schemas.mentors import MentorSchoolRead

router = APIRouter(prefix="/api/v1", tags=["public"])


@router.get(
    "/mentor-schools",
    response_model=Page[MentorSchoolRead],
    summary="Schools that bookable mentors come from",
    description=(
        "The schools mentors you can book now studied at, most mentors first, "
        "ties by name.\n\n"
        "**Public**, no token, and the same answer for everyone: cacheable for "
        "ten minutes.\n\n"
        "**Counted mentors** are those `/mentors` lists with `taking_bookings: "
        "true` (approved, listed, and set up to be booked), once each.\n\n"
        "**A mentor's school** is the institution their card shows (`institution` "
        "on `/mentors`): their highest qualification, then the latest. Only "
        "schools in the platform's catalogue count. A school the mentor typed "
        "that the catalogue does not hold, or one still waiting for review, is "
        "left out, and that mentor is not counted under another school.\n\n"
        "**One entry per name.** Catalogue schools that share a name in "
        "different countries are counted together, so no name appears twice.\n\n"
        f"`limit` is {DEFAULT_SCHOOLS} by default, 1 to {MAX_SCHOOLS}. "
        "`next_cursor` is always `null`; an empty `data` is a real answer."
    ),
    responses={
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": f"`limit` is not a whole number from 1 to {MAX_SCHOOLS}."
        }
    },
)
async def read_mentor_schools(
    schools: MentorSchoolsDep, response: Response
) -> Page[MentorSchoolRead]:
    # The same answer for every viewer; ten minutes because the strip is a
    # social-proof line and a mentor joining or pausing need not show at once.
    response.headers["Cache-Control"] = "public, max-age=600"
    return Page(data=[MentorSchoolRead(name=row["name"]) for row in schools], next_cursor=None)
