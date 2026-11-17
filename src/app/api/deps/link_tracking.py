"""Recording a mentor profile link's visits and shares (decisions 346-348).

Public writes: anyone may send one, a bearer token only says who is looking.
Each answers `204` whether or not anything was stored, so the response says
nothing about de-duplication, the budget or whose profile it is.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request

from app.api.deps.core import OptionalViewerDep, SessionDep
from app.api.schemas.link_tracking import ShareWrite, VisitWrite
from app.core.errors import NotFoundError
from app.domain.activity import activity_day
from app.domain.link_tracking import (
    guest_identity,
    member_identity,
    normalise_referrer_host,
    normalise_via,
    visitor_key,
)
from app.domain.rate_limits import SlidingWindow
from app.infra.db.link_tracking import record_share, record_visit
from app.infra.db.mentor_public_store import get_public_mentor_id
from app.infra.db.visitor_day_keys import day_key

logger = logging.getLogger(__name__)

#: The most of a User-Agent that goes into a guest's key; the rest adds nothing.
MAX_USER_AGENT = 512


@dataclass(frozen=True, slots=True)
class LinkTarget:
    """A live mentor and who is looking at them, ready to record."""

    mentor_id: UUID
    viewer: UUID | None
    day: dt.date
    key: bytes

    @property
    def own(self) -> bool:
        return self.viewer == self.mentor_id


def client_address(request: Request) -> str:
    """The visitor's address: the first `X-Forwarded-For` hop, else the peer.

    Behind the front end's proxy (decision 304) the first hop is the address
    Vercel saw, which it overwrites rather than trusts. Used only inside a
    hashed visitor key, so a forged value can at worst count one person twice.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    first = forwarded.split(",", 1)[0].strip()
    if first:
        return first
    return request.client.host if request.client else ""


async def link_target(
    request: Request, handle: str, session: SessionDep, viewer: OptionalViewerDep
) -> LinkTarget | None:
    """The live mentor ``handle`` names, or ``None`` when there is nothing to count.

    Resolved exactly as `GET /mentors/{handle}` resolves it
    (`get_public_mentor_id`), so the same handles `404`. Only a live mentor is
    counted: a profile only its owner or an admin may see answers `204` and
    records nothing, as it would be `200` to them on the read; so does a
    request whose UTC day another process has already ended (`day_key`).

    The day's key is read from the database on every event and never kept in
    the process past the request (decision 348).
    """
    mentor = await get_public_mentor_id(session, handle)
    if mentor is None:
        if viewer is not None and await get_public_mentor_id(session, handle, viewer):
            return None
        raise NotFoundError("no such mentor")
    day = activity_day(dt.datetime.now(dt.UTC))
    if viewer is not None:
        identity = member_identity(viewer)
    else:
        agent = request.headers.get("user-agent", "")[:MAX_USER_AGENT]
        identity = guest_identity(client_address(request), agent)
    key = await day_key(session, day)
    if key is None:
        return None
    return LinkTarget(mentor, viewer, day, visitor_key(key, mentor, identity))


LinkTargetDep = Annotated[LinkTarget | None, Depends(link_target)]


def _within_budget(request: Request) -> bool:
    """Whether this process may store one more link event now (decision 347).

    Spent, an event is dropped rather than refused, so a flood costs counts,
    never a visitor's page; a warning is logged once per spell.
    """
    budget: SlidingWindow = request.app.state.link_events_budget
    allowed, first_refusal = budget.take(time.monotonic())
    if first_refusal:
        logger.warning("link events over budget; dropping until it frees")
    return allowed


async def visit_recorded(
    request: Request, body: VisitWrite, target: LinkTargetDep, session: SessionDep
) -> None:
    """Record a visit: never the mentor's own, at most one per visitor a day."""
    if target is None or target.own or not _within_budget(request):
        return
    await record_visit(
        session,
        mentor_id=target.mentor_id,
        day=target.day,
        visitor_key=target.key,
        signed_in=target.viewer is not None,
        via=normalise_via(body.via),
        referrer_host=normalise_referrer_host(body.referrer_host),
    )
    await session.commit()


async def share_recorded(
    request: Request, body: ShareWrite, target: LinkTargetDep, session: SessionDep
) -> None:
    """Record a share: at most one per person per method a day, `own` decided
    from the token rather than the body."""
    if target is None or not _within_budget(request):
        return
    await record_share(
        session,
        mentor_id=target.mentor_id,
        day=target.day,
        visitor_key=target.key,
        signed_in=target.viewer is not None,
        method=body.method,
        own=target.own,
    )
    await session.commit()


VisitRecordedDep = Annotated[None, Depends(visit_recorded)]
ShareRecordedDep = Annotated[None, Depends(share_recorded)]
