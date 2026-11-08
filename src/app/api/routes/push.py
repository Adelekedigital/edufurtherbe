"""Web Push (#386, decision 312): the server's public key, and your browsers.

A browser subscribes with the key from `/push/config`, then sends what
`PushSubscription.toJSON()` returns. One row per browser: subscribing a browser
another account holds moves it to you, so whoever signed in there last is the
one it notifies.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Response, status

from app.api.deps.core import PRIVATE_NO_STORE
from app.api.deps.push import PushConfigDep, PushStatusDep, SubscribedDep, UnsubscribedDep
from app.api.schemas.push import PushConfigRead, PushSubscriptionStatus
from app.domain.push import MAX_SUBSCRIPTIONS_PER_USER

router = APIRouter(prefix="/api/v1", tags=["push"])

#: A key changes only on a rotation; five minutes bounds how long a browser
#: may subscribe against the old one.
CONFIG_CACHE = "public, max-age=300"

UNAUTHENTICATED: dict[int | str, dict[str, Any]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "The bearer token is absent, malformed, expired or wrongly signed."
    },
}
SUBS = "/api/v1/me/push-subscriptions"

INVALID_SUBSCRIPTION: dict[int | str, dict[str, Any]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "description": (
            "The body failed validation: an endpoint that is not a known push "
            "service is reported at `/endpoint`, a malformed key at `/keys/...`."
        )
    }
}
INVALID_ENDPOINT: dict[int | str, dict[str, Any]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {
        "description": "`endpoint` is missing, empty or too long."
    }
}


@router.get(
    "/push/config",
    response_model=PushConfigRead,
    summary="The push public key",
    description=(
        "What a browser needs to subscribe. No sign-in. `503` "
        "(`/problems/push-unavailable`) when push is not set up here: hide the opt-in."
    ),
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "`/problems/push-unavailable`: push is not set up here."
        }
    },
)
def read_push_config(config: PushConfigDep, response: Response) -> PushConfigRead:
    response.headers["Cache-Control"] = CONFIG_CACHE
    return config


@router.post(
    "/me/push-subscriptions",
    status_code=status.HTTP_201_CREATED,
    response_model=PushSubscriptionStatus,
    summary="Subscribe this browser",
    description=(
        "Stores this browser's subscription as yours: `201` when it is new, "
        "`200` when the endpoint was already stored (yours, or another account's "
        "on this browser, which it now stops notifying). The keys are replaced "
        f"either way. You keep at most {MAX_SUBSCRIPTIONS_PER_USER} browsers; "
        "subscribing another drops the one least recently subscribed. "
        "`Location` is where to ask whether this browser is still subscribed."
    ),
    responses=UNAUTHENTICATED
    | INVALID_SUBSCRIPTION
    | {
        status.HTTP_200_OK: {
            "model": PushSubscriptionStatus,
            "description": "Already stored; now yours, with these keys.",
        }
    },
)
async def subscribe_browser(done: SubscribedDep, response: Response) -> PushSubscriptionStatus:
    if not done.created:
        response.status_code = status.HTTP_200_OK
    response.headers["Location"] = f"{SUBS}?endpoint={quote(done.endpoint, safe='')}"
    response.headers["Cache-Control"] = PRIVATE_NO_STORE
    return PushSubscriptionStatus(subscribed=True)


@router.delete(
    "/me/push-subscriptions",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unsubscribe this browser",
    description=(
        "Stops pushes to this browser. `204` whether or not it was subscribed, "
        "and an endpoint that is somebody else's is left alone. Call it before "
        "signing out: it needs the token. No host check here, so a browser "
        "subscribed under an older rule can always be removed."
    ),
    responses=UNAUTHENTICATED | INVALID_ENDPOINT,
)
async def unsubscribe_browser(_: UnsubscribedDep) -> None:
    return None


@router.get(
    "/me/push-subscriptions",
    response_model=PushSubscriptionStatus,
    summary="Whether this browser is subscribed",
    description="Whether this endpoint is subscribed as you, for the Settings switch.",
    responses=UNAUTHENTICATED | INVALID_ENDPOINT,
)
async def read_push_status(state: PushStatusDep, response: Response) -> PushSubscriptionStatus:
    response.headers["Cache-Control"] = PRIVATE_NO_STORE
    return state
