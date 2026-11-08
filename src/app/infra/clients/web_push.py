"""Sending one Web Push to one browser (#386, decision 312).

The payload is encrypted to the browser's keys (RFC 8291, `aes128gcm`, by
`http_ece`) and the request carries a VAPID token signed with the server's key
(RFC 8292, ES256 by `pyjwt`). Sent through the shared client, synchronous like
every adapter, so callers reach it through `asyncio.to_thread`.

**The endpoint is checked again here** (`is_push_service`) before any request:
a row written under an older allowlist, or by anything that bypassed the API,
is never sent to. The shared client follows no redirects, so a push service
cannot bounce the request elsewhere either. Neither the endpoint nor a key is
ever logged: the push service's host is enough to diagnose a failure.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlsplit

import http_ece
import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec

from app.core.config import Settings, base64url_bytes
from app.domain.push import is_push_service
from app.infra.http.client import shared

__all__ = [
    "NullWebPush",
    "PushOutcome",
    "PushTarget",
    "WebPushPort",
    "WebPushSender",
    "web_push_for",
]

logger = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(10.0)

#: How long a VAPID token is good for. RFC 8292 caps it at 24 hours; half a
#: day leaves room for a clock that is ahead.
TOKEN_LIFETIME = dt.timedelta(hours=12)


class PushOutcome(StrEnum):
    #: The push service took it.
    DELIVERED = "delivered"
    #: The subscription is gone (`404`/`410`): forget the browser.
    GONE = "gone"
    #: Anything else: keep the browser, this push is lost.
    FAILED = "failed"


@dataclass(frozen=True)
class PushTarget:
    """One browser to push to: a stored subscription."""

    endpoint: str
    p256dh: str
    auth: str


class WebPushPort(Protocol):
    def send(
        self, target: PushTarget, payload: bytes, *, ttl: dt.timedelta, urgency: str
    ) -> PushOutcome: ...


class NullWebPush:
    """Push is not configured here: nothing is sent, and nothing is forgotten."""

    def send(
        self, target: PushTarget, payload: bytes, *, ttl: dt.timedelta, urgency: str
    ) -> PushOutcome:
        del target, payload, ttl, urgency
        return PushOutcome.FAILED


class WebPushSender:
    """Encrypts, signs and sends a push."""

    def __init__(
        self,
        *,
        public_key: str,
        private_key: bytes,
        subject: str,
        client: httpx.Client | None = None,
    ) -> None:
        self._public_key = public_key.rstrip("=")
        self._signer = ec.derive_private_key(int.from_bytes(private_key, "big"), ec.SECP256R1())
        self._subject = subject
        self._client = client or shared(TIMEOUT)

    def _authorization(self, endpoint: str, now: dt.datetime) -> str:
        # The host `is_push_service` checked: lowercased, no trailing dot. Port
        # and userinfo are already refused, so this is the whole origin.
        host = (urlsplit(endpoint).hostname or "").rstrip(".")
        token = jwt.encode(
            {
                "aud": f"https://{host}",
                "exp": int((now + TOKEN_LIFETIME).timestamp()),
                "sub": self._subject,
            },
            self._signer,
            algorithm="ES256",
        )
        return f"vapid t={token}, k={self._public_key}"

    def send(
        self, target: PushTarget, payload: bytes, *, ttl: dt.timedelta, urgency: str
    ) -> PushOutcome:
        host = urlsplit(target.endpoint).hostname
        if not is_push_service(target.endpoint):
            logger.warning("push refused: not a push service", extra={"push_host": host})
            return PushOutcome.FAILED
        dh, auth = base64url_bytes(target.p256dh), base64url_bytes(target.auth)
        if dh is None or auth is None:
            logger.warning("push refused: unreadable keys", extra={"push_host": host})
            return PushOutcome.FAILED
        try:
            body = http_ece.encrypt(
                payload,
                private_key=ec.generate_private_key(ec.SECP256R1()),
                dh=dh,
                auth_secret=auth,
                version="aes128gcm",
            )
        except ValueError:
            # A key that is not a point on the curve: the browser's row is bad.
            logger.warning("push refused: invalid keys", extra={"push_host": host})
            return PushOutcome.FAILED
        try:
            response = self._client.post(
                target.endpoint,
                content=body,
                headers={
                    "Authorization": self._authorization(target.endpoint, dt.datetime.now(dt.UTC)),
                    "Content-Encoding": "aes128gcm",
                    "Content-Type": "application/octet-stream",
                    "TTL": str(int(ttl.total_seconds())),
                    "Urgency": urgency,
                },
            )
        except httpx.HTTPError as exc:
            logger.warning("push not sent", extra={"push_host": host, "error": type(exc).__name__})
            return PushOutcome.FAILED
        if response.status_code in (404, 410):
            return PushOutcome.GONE
        if response.is_success:
            return PushOutcome.DELIVERED
        logger.warning("push refused", extra={"push_host": host, "status": response.status_code})
        return PushOutcome.FAILED


def web_push_for(settings: Settings) -> WebPushPort:
    """The sender when push is configured, otherwise `NullWebPush`. The one
    place either is chosen."""
    public, private, subject = (
        settings.vapid_public_key,
        settings.vapid_private_key,
        settings.vapid_subject,
    )
    if public is None or private is None or subject is None:
        return NullWebPush()
    key = base64url_bytes(private.get_secret_value())
    if key is None:  # the settings validator has already refused this
        return NullWebPush()
    return WebPushSender(public_key=public, private_key=key, subject=subject)
