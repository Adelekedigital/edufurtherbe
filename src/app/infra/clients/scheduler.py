"""Asking to be called back at a time, and proving the caller was us.

**Why a scheduler at all**, when this codebase already has an hourly sweep: the
sweep cannot deliver a reminder twelve hours before a deadline to within any
useful precision, and a tighter cron is unreliable however short — GitHub
Actions schedules are documented as delayable under load. Per-message scheduling
is precise where polling is only as good as its interval (ADR 0025).

**QStash rather than a platform primitive.** Settled decision #13 keeps the
FastAPI Cloud → Railway exit real by using no *platform-native* queue or cron;
QStash is vendor-neutral HTTP, so the exit is untouched. It was already in
``pyproject.toml``'s vendor allowlist before anything used it.

**Nothing is ever cancelled**, which is the design that makes this safe. A
scheduled callback for a session that has since been answered simply does
nothing when it arrives — see the callback route. The alternative makes four
transitions responsible for unscheduling, and the bug is the message that fires
for a request answered through the one path somebody forgot.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import logging
from typing import Any

import httpx
import jwt

from app.core.errors import AppError

__all__ = [
    "NullScheduler",
    "QStashScheduler",
    "SchedulerError",
    "UntrustedCallbackError",
    "verify_callback",
]

logger = logging.getLogger(__name__)

QSTASH_PUBLISH = "https://qstash.upstash.io/v2/publish"

#: Long enough for a slow third party, short enough that scheduling cannot hold
#: a booking open. A reminder that fails to schedule is recoverable; a booking
#: that hangs on one is not.
TIMEOUT = httpx.Timeout(15.0)

#: QStash signs with HS256, the same algorithm `SupabaseTokenVerifier` already
#: handles for the symmetric case — so this is a second instance of a pattern
#: rather than a new one.
ALGORITHM = "HS256"


class SchedulerError(AppError):
    """A callback could not be scheduled."""


class UntrustedCallbackError(AppError):
    """A request claiming to be a scheduled callback was not one.

    Its own type rather than an authentication error, because the two are not
    the same failure: `AuthenticationError` means a *person's* token did not
    check out, and this means a request arrived at a public endpoint carrying a
    signature we did not issue.
    """


class NullScheduler:
    """Schedules nothing and says so. The default.

    Not a fake: it is the state of a deployment with no QStash token, where
    reminders simply do not fire. Booking still works, the deadline still
    passes, and the expiry sweep still frees the slot — the mentor is just
    never nudged.
    """

    def schedule(self, *, url: str, body: dict[str, Any], at: dt.datetime) -> None:
        # `url` is named and unused on purpose: the signature is the contract
        # the caller is written against, and trimming it to what a null
        # implementation touches would make the real scheduler's arrival a
        # signature change at the call site.
        del url
        logger.info("no scheduler configured; not scheduling %s for %s", body, at.isoformat())


class QStashScheduler:
    """Publishes a callback to fire at an instant.

    **`Upstash-Not-Before` rather than a delay**, because a delay is computed
    from *now* and would drift by however long the request took to reach QStash.
    An instant is the thing the product actually specifies — twelve hours before
    a deadline — and it survives a retry of the publish itself.
    """

    def __init__(self, token: str, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(
            headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT
        )

    def schedule(self, *, url: str, body: dict[str, Any], at: dt.datetime) -> None:
        """Ask to be called at ``url`` with ``body``, not before ``at``.

        A time already past is published anyway rather than refused: QStash
        delivers it immediately, which is the right answer for a reminder whose
        moment arrived while the booking was being written.
        """
        try:
            response = self._client.post(
                f"{QSTASH_PUBLISH}/{url}",
                json=body,
                headers={"Upstash-Not-Before": str(int(at.timestamp()))},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SchedulerError(f"qstash refused a callback for {at.isoformat()}: {exc}") from exc


def verify_callback(*, token: str, body: bytes, url: str, signing_keys: tuple[str, ...]) -> None:
    """Prove a callback came from QStash, or raise.

    **Two checks, and the second is the one that matters.** A valid signature
    says QStash issued *a* token; the body hash says it issued one for *this
    payload*. Without the hash an attacker who observed one callback could
    replay its signature against any body they liked — and the endpoint writes
    to the outbox, so that is a message-sending primitive.

    **Two keys, because QStash rotates them.** The current one is tried first
    and the next one after, so a rotation does not drop callbacks in the window
    where both are live. A deployment configuring only one passes a
    single-element tuple.

    ``sub`` is checked against the URL we expect, so a token minted for a
    different endpoint of ours cannot be replayed at this one.
    """
    expected = base64.urlsafe_b64encode(hashlib.sha256(body).digest()).decode().rstrip("=")

    for key in signing_keys:
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[ALGORITHM],
                issuer="Upstash",
                options={"require": ["exp", "nbf", "iss", "sub"]},
            )
        except jwt.PyJWTError:
            continue

        if str(claims.get("sub", "")).rstrip("/") != url.rstrip("/"):
            raise UntrustedCallbackError("this callback was signed for a different endpoint")
        # QStash pads its hash; ours is stripped, so compare on the stripped form
        # rather than requiring the two to agree on padding.
        if str(claims.get("body", "")).rstrip("=") != expected:
            raise UntrustedCallbackError("the callback body does not match its signature")
        return

    raise UntrustedCallbackError("no configured signing key verifies this callback")
