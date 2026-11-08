"""Web Push: the public key, and the caller's own subscriptions (decision 312).

`CurrentUserDep` is the only identity: nothing takes a user from the path or
the body, and the store names the caller in every statement it scopes.
Preview as user: POST and DELETE are refused by the central read-only guard,
and the GET by `RefuseInPreviewDep`: whether an admin's browser is subscribed
as the previewed user is not something a preview shows.
"""

from __future__ import annotations

from typing import Annotated, NamedTuple

from fastapi import Depends, Query, Request

from app.api.deps.core import CurrentUserDep, RefuseInPreviewDep, SessionDep, _configured
from app.api.schemas.push import (
    PushConfigRead,
    PushSubscriptionCreate,
    PushSubscriptionDelete,
    PushSubscriptionStatus,
)
from app.core.errors import PushUnavailableError
from app.domain.push import ENDPOINT_MAX_LENGTH
from app.infra.db.push_store import subscribe, subscribed, unsubscribe

__all__ = ["PushConfigDep", "PushStatusDep", "SubscribedDep", "UnsubscribedDep"]

UNAVAILABLE = "push notifications are not set up here"


def push_config(request: Request) -> PushConfigRead:
    """The public key, or `503` when push is not configured."""
    settings = _configured(request)
    if settings.vapid_public_key is None:  # all three or none: `push_configured`
        raise PushUnavailableError(UNAVAILABLE)
    return PushConfigRead(vapid_public_key=settings.vapid_public_key)


class Subscribed(NamedTuple):
    """What a subscribe did: whether the row is new, and for which endpoint."""

    created: bool
    endpoint: str


async def subscribed_browser(
    body: PushSubscriptionCreate, user: CurrentUserDep, session: SessionDep
) -> Subscribed:
    """Subscribe this browser as the caller. Commits.

    Returns the endpoint too, so the route need not declare the body again:
    a body declared twice is validated twice, and every `422` lists its errors
    twice."""
    created = await subscribe(
        session, user["id"], endpoint=body.endpoint, p256dh=body.keys.p256dh, auth=body.keys.auth
    )
    await session.commit()
    return Subscribed(created, body.endpoint)


async def unsubscribed_browser(
    body: PushSubscriptionDelete, user: CurrentUserDep, session: SessionDep
) -> None:
    """Unsubscribe this browser if it is the caller's. Commits."""
    await unsubscribe(session, user["id"], body.endpoint)
    await session.commit()


async def push_status(
    user: CurrentUserDep,
    session: SessionDep,
    refused: RefuseInPreviewDep,
    endpoint: Annotated[
        str,
        Query(
            min_length=1,
            max_length=ENDPOINT_MAX_LENGTH,
            description="This browser's endpoint, from `PushSubscription.endpoint`.",
        ),
    ],
) -> PushSubscriptionStatus:
    del refused
    return PushSubscriptionStatus(subscribed=await subscribed(session, user["id"], endpoint))


PushConfigDep = Annotated[PushConfigRead, Depends(push_config)]
SubscribedDep = Annotated[Subscribed, Depends(subscribed_browser)]
UnsubscribedDep = Annotated[None, Depends(unsubscribed_browser)]
PushStatusDep = Annotated[PushSubscriptionStatus, Depends(push_status)]
