"""Weekly hours, blocked dates, and the public bookable slots."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Query, Request

from app.api.deps.calendar import _free_busy
from app.api.deps.core import OwnerDep, SessionDep, TargetUserDep
from app.api.schemas.availability import (
    AvailabilityExceptionWrite,
    AvailabilityRulePatch,
    AvailabilityRuleWrite,
)
from app.core.errors import (
    NotFoundError,
)
from app.domain.availability import DEFAULT_PROJECTION_DAYS, UtcInterval
from app.infra.db.availability_store import list_exceptions, list_rules
from app.infra.db.availability_writer import (
    create_exception,
    create_rule,
    delete_exception,
    delete_rule,
    update_rule,
)

# `get_session` is aliased: this module already has one, and it is the **database
# session** dependency at line 142. Two callables with that name in one file is a
# collision a reader resolves by scrolling, and the wrong one is a plausible
# mistake rather than an obvious error — `bubble_id` shadowed a local the same
# way in the M4 transform and raised `UnboundLocalError` far from the edit.
from app.infra.db.slot_store import list_slots

# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------
#
# Reads take `TargetUserDep` and writes take `OwnerDep`, which is the one clause
# that separates "an admin reviewing a mentor's schedule" from "an admin
# silently editing it". The pair differ only by their name at the call site, so
# a reader comparing the two routes sees it without opening this module.
#
# **These stay owner-and-admin, and are no longer waiting for anything.** They
# were narrowed pending D20's middle clause — render if the viewer has a session
# with this mentor — which needed a `sessions` table to scope against. That table
# arrived, and settled decision #94 then **dropped the clause**: a mentee with a
# session sees *that session*, which carries the mentor's name since #93, so
# nothing breaks when a mentor pauses. What a stranger may see about a mentor is
# `GET /mentors/{handle}`; these routes answer a different question — what has
# this mentor *declared* — and that was only ever theirs and an admin's.


async def target_availability_rules(
    user_id: TargetUserDep, session: SessionDep
) -> list[dict[str, Any]]:
    return await list_rules(session, user_id)


async def target_availability_exceptions(
    user_id: TargetUserDep, session: SessionDep
) -> list[dict[str, Any]]:
    return await list_exceptions(session, user_id)


async def created_availability_rule(
    payload: AvailabilityRuleWrite, user_id: OwnerDep, session: SessionDep
) -> UUID:
    rule_id = await create_rule(session, user_id, payload.model_dump())
    await session.commit()
    return rule_id


async def updated_availability_rule(
    rule_id: UUID, payload: AvailabilityRulePatch, user_id: OwnerDep, session: SessionDep
) -> bool:
    changed = await update_rule(session, user_id, rule_id, payload.model_dump(exclude_unset=True))
    await session.commit()
    return changed


async def deleted_availability_rule(rule_id: UUID, user_id: OwnerDep, session: SessionDep) -> bool:
    removed = await delete_rule(session, user_id, rule_id)
    await session.commit()
    return removed


async def created_availability_exception(
    payload: AvailabilityExceptionWrite, user_id: OwnerDep, session: SessionDep
) -> UUID:
    exception_id = await create_exception(session, user_id, payload.model_dump())
    await session.commit()
    return exception_id


async def deleted_availability_exception(
    exception_id: UUID, user_id: OwnerDep, session: SessionDep
) -> bool:
    removed = await delete_exception(session, user_id, exception_id)
    await session.commit()
    return removed


AvailabilityRulesDep = Annotated[list[dict[str, Any]], Depends(target_availability_rules)]
AvailabilityExceptionsDep = Annotated[list[dict[str, Any]], Depends(target_availability_exceptions)]
CreatedAvailabilityRuleDep = Annotated[UUID, Depends(created_availability_rule)]
UpdatedAvailabilityRuleDep = Annotated[bool, Depends(updated_availability_rule)]
DeletedAvailabilityRuleDep = Annotated[bool, Depends(deleted_availability_rule)]
CreatedAvailabilityExceptionDep = Annotated[UUID, Depends(created_availability_exception)]
DeletedAvailabilityExceptionDep = Annotated[bool, Depends(deleted_availability_exception)]


# --------------------------------------------------------------------------
# Bookable slots
# --------------------------------------------------------------------------
#
# **The one dependency in this module with no viewer.** Every other read here
# resolves a caller and scopes to them; this one is public, and what stands in
# place of a viewer is the mentor's own state — approved *and* listed, checked
# inside the query. The absence of `CurrentUserDep` below is the whole
# authorization decision, so it is stated rather than left to be noticed.


async def mentor_slots(
    request: Request,
    user_id: UUID,
    session: SessionDep,
    session_type_id: Annotated[UUID, Query(description="Which offering to price the slots for.")],
    start: Annotated[
        dt.date | None,
        Query(description="First day, in the mentor's timezone. Defaults to their today."),
    ] = None,
    end: Annotated[
        dt.date | None,
        Query(
            description=(
                "Day after the last, exclusive. Defaults to "
                f"{DEFAULT_PROJECTION_DAYS} days after `start`."
            )
        ),
    ] = None,
) -> list[UtcInterval]:
    """Slots someone could book, or a 404 that says nothing about why.

    **`now` is read here and passed down**, rather than inside the store. The
    notice window makes this answer depend on the clock, and a function reading
    its own clock cannot be tested against a DST boundary without moving the
    machine's timezone.

    **The dates are not defaulted or validated here**, though this is the edge
    and that is where validation usually belongs. An omitted `start` means the
    mentor's today, which needs the mentor's timezone — so the default is only
    knowable after the query that finds them, and a range's legality depends on
    the default. Splitting the two would put half a rule in each layer.

    `session_type_id` stays **required**. A slot's length and notice window come
    from the offering, so "when is this mentor free" has no answer without one.
    Falling back to "their only offering" would break every caller that omitted
    it on the day a mentor adds a second — someone else's edit breaking an
    integration that did not change.
    """
    slots = await list_slots(
        session,
        user_id,
        session_type_id,
        start=start,
        end=end,
        now=dt.datetime.now(dt.UTC),
        external_busy=_free_busy(request),
    )
    if slots is None:
        raise NotFoundError("no such bookable session type")
    return slots


SlotsDep = Annotated[list[UtcInterval], Depends(mentor_slots)]
