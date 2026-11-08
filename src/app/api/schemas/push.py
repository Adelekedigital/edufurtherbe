"""Web Push: what `/push/config` and `/me/push-subscriptions` take and return (decision 312).

The subscription body is what `PushSubscription.toJSON()` gives a browser, sent
as it is. No body returns an endpoint or a key: the client already has them,
and a response is one more place to leak them from.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.push import (
    AUTH_MAX_LENGTH,
    ENDPOINT_MAX_LENGTH,
    KEY_PATTERN,
    P256DH_MAX_LENGTH,
    PUSH_SERVICE_HOSTS,
    is_push_service,
)

__all__ = [
    "PushConfigRead",
    "PushKeys",
    "PushSubscriptionCreate",
    "PushSubscriptionDelete",
    "PushSubscriptionStatus",
]

_HOSTS = ", ".join(f"`{host}`" for host in PUSH_SERVICE_HOSTS)


class PushConfigRead(BaseModel):
    """What a browser needs to subscribe."""

    vapid_public_key: str = Field(
        description=(
            "The server's VAPID public key, base64url: pass it to "
            "`pushManager.subscribe` as `applicationServerKey`. It changes only "
            "on a rotation, after which every browser must subscribe again."
        )
    )


class PushKeys(BaseModel):
    """The keys a payload to this browser is encrypted to (RFC 8291)."""

    model_config = ConfigDict(extra="forbid")

    p256dh: str = Field(min_length=1, max_length=P256DH_MAX_LENGTH, pattern=KEY_PATTERN)
    auth: str = Field(min_length=1, max_length=AUTH_MAX_LENGTH, pattern=KEY_PATTERN)


class PushSubscriptionCreate(BaseModel):
    """This browser's subscription, as `PushSubscription.toJSON()` gives it."""

    model_config = ConfigDict(extra="forbid")

    endpoint: str = Field(
        max_length=ENDPOINT_MAX_LENGTH,
        description=(
            "The push service's URL for this browser. Must be `https` on one of "
            f"{_HOSTS} or a subdomain of one; anything else is a `422` at `/endpoint`."
        ),
    )
    keys: PushKeys
    expirationTime: int | None = Field(  # noqa: N815 - the browser's own key
        default=None, description="Sent by `toJSON()`; accepted and ignored."
    )

    @field_validator("endpoint")
    @classmethod
    def a_known_push_service(cls, value: str) -> str:
        if not is_push_service(value):
            raise ValueError("not a push service endpoint this platform sends to")
        return value


class PushSubscriptionDelete(BaseModel):
    """The browser to stop pushing to."""

    model_config = ConfigDict(extra="forbid")

    endpoint: str = Field(
        min_length=1,
        max_length=ENDPOINT_MAX_LENGTH,
        description="This browser's endpoint, as subscribed.",
    )


class PushSubscriptionStatus(BaseModel):
    """Whether a browser is subscribed as you."""

    subscribed: bool = Field(
        description="`true` when this endpoint is subscribed to your account on this platform."
    )
