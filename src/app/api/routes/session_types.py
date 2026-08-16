"""A mentor's session types, to anyone who asks.

The second public endpoint, and the one that makes the first usable: `/slots`
requires a `session_type_id` and nothing handed one out until this shipped.

`tags=["public"]` per settled decision #64, in its own module for the same
reason `routes/slots.py` is — so which endpoints take no token is visible in the
file list rather than in one decorator among ten.

**Session type, not "offering" and not "service".** `service_offerings` is the
closed six-row taxonomy already public at `/api/v1/catalog/service-offerings`,
and it is the axis matching joins on. Using either word here would put two
meanings of one term in the same API.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import SessionTypesDep
from app.api.schemas.common import Page
from app.api.schemas.session_types import SessionTypeRead

router = APIRouter(prefix="/api/v1/users/{user_id}", tags=["public"])

PUBLIC_RESPONSES: dict[int | str, dict[str, str]] = {
    status.HTTP_404_NOT_FOUND: {
        "description": (
            "No such mentor, **or** they are not publicly visible — unapproved, "
            "unlisted, or not a mentor at all. Indistinguishable on purpose: "
            "telling them apart says which mentors exist and what state they "
            "are in.\n\nA mentor who *is* visible but currently offers nothing "
            "gets a `200` with an empty page instead. That is a different and "
            "true statement."
        )
    }
}


@router.get(
    "/session-types",
    response_model=Page[SessionTypeRead],
    summary="What a mentor offers",
    description=(
        "Everything this mentor currently offers to book, with the duration and "
        "notice that govern each one.\n\n"
        "**Public.** No token is required — a mentee compares mentors before "
        "signing up. A mentor appears here only while approved **and** listed; "
        "pausing removes them from this endpoint as well as from search.\n\n"
        "Take an `id` from here and pass it as `session_type_id` to "
        "`/api/v1/users/{user_id}/availability/slots` to see when that offering "
        "can actually be booked.\n\n"
        "**A session type is not a service offering.** The six-row taxonomy at "
        "`/api/v1/catalog/service-offerings` describes what *kind* of help "
        "exists and is what matching joins on; this is one mentor's own "
        "bookable product.\n\n"
        "`meeting_venue` is read straight off the offering — **every one "
        "carries its own**, and there is no cascade from the mentor for a "
        "client to know about. The meeting link itself is generated per "
        "session and never appears here.\n\n"
        "Switched-off and deleted offerings are absent; `is_active` covers off, "
        "closed and hidden alike.\n\n"
        "`next_cursor` is always `null`. A mentor holds a handful of offerings, "
        "so the answer is returned whole; the envelope is here because ADR 0016 "
        "puts it on every list."
    ),
    responses=PUBLIC_RESPONSES,
)
async def list_mentor_session_types(session_types: SessionTypesDep) -> Page[SessionTypeRead]:
    return Page(data=[SessionTypeRead.from_row(row) for row in session_types], next_cursor=None)
