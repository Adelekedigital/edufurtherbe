"""A mentor's own availability: recurring rules, and dated exceptions.

**This is what a mentor declares, not what a mentee can book.** A bookable slot
is this, minus everything already booked, sliced into a session type's duration
and filtered by notice — which is `routes/slots.py`, added once M4 gave it
sessions to subtract. The external calendar is still to come and subtracts from
the same intervals, so it changes that endpoint and not these.

These routes stay **owner-and-admin**; only `/slots` is public.

Addressed by user id rather than `/me/…`, following the other attribute routes:
one implementation serves a mentor editing their own schedule and an admin
reviewing it, and the only difference is which dependency the handler asks for.
`TargetUserDep` admits an admin; `OwnerDep` does not.

**Reads are owner-and-admin only, deliberately narrower than the eventual rule.**
D20 says a profile renders if the mentor is listed, *or* the viewer has a session
with them, *or* the viewer is an admin — and the middle clause has no table until
M4. Shipping the two that exist would drop precisely the one protecting a mentee
whose mentor has since paused, which is the case D20 exists for. Widening later
is additive; narrowing after a client has built against it is not.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.api.deps import (
    AvailabilityExceptionsDep,
    AvailabilityRulesDep,
    CreatedAvailabilityExceptionDep,
    CreatedAvailabilityRuleDep,
    DeletedAvailabilityExceptionDep,
    DeletedAvailabilityRuleDep,
    UpdatedAvailabilityRuleDep,
)
from app.api.schemas.availability import AvailabilityExceptionRead, AvailabilityRuleRead
from app.core.errors import NotFoundError

router = APIRouter(prefix="/api/v1/users/{user_id}/availability", tags=["availability"])

READ_RESPONSES: dict[int | str, dict[str, str]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "The bearer token is absent, malformed, expired or wrongly signed."
    },
    status.HTTP_404_NOT_FOUND: {
        "description": (
            "No such user, **or** not yours. Indistinguishable on purpose — "
            "distinguishing them tells anyone who can enumerate ids which users "
            "exist. A platform admin may read these."
        )
    },
}

WRITE_RESPONSES: dict[int | str, dict[str, str]] = {
    **READ_RESPONSES,
    status.HTTP_404_NOT_FOUND: {
        "description": (
            "No such user or record, **or** not yours. Indistinguishable on "
            "purpose. **A platform admin also receives 404 here** — admins read "
            "these records and do not write them."
        )
    },
    status.HTTP_409_CONFLICT: {
        "description": (
            "This window overlaps one the mentor already has on that weekday. "
            "Two overlapping windows say the same thing twice, so the database "
            "refuses the pair; widen the existing rule rather than adding a "
            "second."
        )
    },
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "description": (
            "The body failed validation — an unknown IANA timezone, a weekday "
            "outside 0-6, or a window whose end is not after its start."
        )
    },
}


@router.get(
    "/rules",
    summary="List recurring availability",
    description=(
        "A mentor's weekly windows, as **declared**: a wall-clock time plus an "
        "IANA zone, never an instant. A recurring rule has not named a date, so "
        "there is nothing to convert — and converting it would bake in one "
        "date's UTC offset, which is the bug that breaks twice a year.\n\n"
        "`day_of_week` is **0 for Sunday**. Several rows on one weekday are "
        "normal: a morning and an afternoon with a lunch gap are two rules, and "
        "the legacy one-row-per-day shape could not express that."
    ),
    responses=READ_RESPONSES,
)
async def list_availability_rules(rules: AvailabilityRulesDep) -> list[AvailabilityRuleRead]:
    return [AvailabilityRuleRead.from_row(row) for row in rules]


@router.post(
    "/rules",
    status_code=status.HTTP_201_CREATED,
    summary="Add a recurring window",
    description=(
        "One weekly window. **Overlapping an existing window on the same "
        "weekday is refused with 409** — the two would say the same thing "
        "twice, and editing one copy later would silently change availability "
        "the other still covers.\n\n"
        "A window crossing midnight is two rules, one per weekday. The server "
        "will not split it: which side of midnight was meant is not something "
        "it can know."
    ),
    responses=WRITE_RESPONSES,
)
async def add_availability_rule(
    created: CreatedAvailabilityRuleDep, user_id: str, response: Response
) -> dict[str, str]:
    response.headers["Location"] = f"/api/v1/users/{user_id}/availability/rules/{created}"
    return {"id": str(created)}


@router.patch(
    "/rules/{rule_id}",
    summary="Change a recurring window",
    description=(
        "Only the fields sent are changed. Absent is not null.\n\n"
        "Moving a window onto one of the mentor's others is refused with 409, "
        "the same as adding an overlapping one."
    ),
    responses=WRITE_RESPONSES,
)
async def edit_availability_rule(changed: UpdatedAvailabilityRuleDep) -> dict[str, bool]:
    if not changed:
        raise NotFoundError("no such availability rule")
    return {"updated": True}


@router.delete(
    "/rules/{rule_id}",
    summary="Remove a recurring window",
    description=(
        "Soft delete. The window stops being offered and stops blocking the "
        "slot it occupied, so the mentor can immediately declare a different "
        "one covering the same hours."
    ),
    responses=WRITE_RESPONSES,
)
async def remove_availability_rule(removed: DeletedAvailabilityRuleDep) -> dict[str, bool]:
    if not removed:
        raise NotFoundError("no such availability rule")
    return {"deleted": True}


@router.get(
    "/exceptions",
    summary="List dated exceptions",
    description=(
        "Dates on which the weekly rules do not apply as written. `block` "
        "removes availability — a holiday, an exam period; `override` adds a "
        "window no rule describes.\n\n"
        "`end_date` is **exclusive**, so a single blocked day spans `d` to "
        "`d + 1`. Null times mean the whole day, in the exception's own "
        "timezone — which is 23 or 25 hours on a day the clocks change."
    ),
    responses=READ_RESPONSES,
)
async def list_availability_exceptions(
    exceptions: AvailabilityExceptionsDep,
) -> list[AvailabilityExceptionRead]:
    return [AvailabilityExceptionRead.from_row(row) for row in exceptions]


@router.post(
    "/exceptions",
    status_code=status.HTTP_201_CREATED,
    summary="Add a dated exception",
    description=(
        "Send `start_time` and `end_time` together for part of a day, or "
        "neither for all of it — a start with no end has no defensible reading."
        "\n\n**A `block` always wins over an `override` on the same date.** A "
        "mentor marking themselves away is never silently overridden by an "
        "older one-off: the cost of being wrong that way is a mentor with no "
        "booking, and the other way it is a mentor booked on their holiday."
    ),
    responses=WRITE_RESPONSES,
)
async def add_availability_exception(
    created: CreatedAvailabilityExceptionDep, user_id: str, response: Response
) -> dict[str, str]:
    response.headers["Location"] = f"/api/v1/users/{user_id}/availability/exceptions/{created}"
    return {"id": str(created)}


@router.delete(
    "/exceptions/{exception_id}",
    summary="Remove a dated exception",
    description="Soft delete. The dates it covered return to whatever the weekly rules say.",
    responses=WRITE_RESPONSES,
)
async def remove_availability_exception(
    removed: DeletedAvailabilityExceptionDep,
) -> dict[str, bool]:
    if not removed:
        raise NotFoundError("no such availability exception")
    return {"deleted": True}
