"""One Web Push, as a push service and the browser would see it (decision 312).

The browser half is real: a P-256 key and an auth secret, and `http_ece` to
decrypt with them, so a payload the browser could not read fails here.
"""

from __future__ import annotations

import base64
import datetime as dt
import os
from typing import Any

import http_ece
import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from tests.unit.test_push_config import vapid_pair

from app.core.config import Settings, base64url_bytes
from app.infra.clients.web_push import (
    NullWebPush,
    PushOutcome,
    PushTarget,
    WebPushSender,
    web_push_for,
)

ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc"
SUBJECT = "mailto:ops@example.test"
TTL = dt.timedelta(days=1)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Browser:
    """The receiving half: its key pair and auth secret."""

    def __init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.auth = os.urandom(16)

    def target(self, endpoint: str = ENDPOINT) -> PushTarget:
        point = self.key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        return PushTarget(endpoint=endpoint, p256dh=_b64(point), auth=_b64(self.auth))

    def read(self, body: bytes) -> bytes:
        return bytes(
            http_ece.decrypt(body, private_key=self.key, auth_secret=self.auth, version="aes128gcm")
        )


def sender(handler: Any) -> tuple[WebPushSender, str]:
    public, private = vapid_pair()
    raw = base64url_bytes(private)
    assert raw is not None
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return WebPushSender(public_key=public, private_key=raw, subject=SUBJECT, client=client), public


def test_the_browser_can_read_the_payload_and_the_service_can_check_the_token() -> None:
    seen: list[httpx.Request] = []
    browser = Browser()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201)

    push, public = sender(handler)
    outcome = push.send(browser.target(), b'{"type":"message"}', ttl=TTL, urgency="normal")

    assert outcome is PushOutcome.DELIVERED
    [request] = seen
    assert str(request.url) == ENDPOINT
    assert browser.read(request.content) == b'{"type":"message"}'
    assert request.headers["content-encoding"] == "aes128gcm"
    assert request.headers["ttl"] == "86400"
    assert request.headers["urgency"] == "normal"
    scheme, _, rest = request.headers["authorization"].partition(" ")
    token, key = (part.split("=", 1)[1] for part in rest.split(", "))
    assert scheme == "vapid"
    assert key == public
    point = base64url_bytes(public)
    assert point is not None
    verifying = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
    claims = jwt.decode(
        token, verifying, algorithms=["ES256"], audience="https://fcm.googleapis.com"
    )
    assert claims["sub"] == SUBJECT
    assert claims["exp"] <= (dt.datetime.now(dt.UTC) + dt.timedelta(hours=24)).timestamp()


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (200, PushOutcome.DELIVERED),
        (202, PushOutcome.DELIVERED),
        (404, PushOutcome.GONE),
        (410, PushOutcome.GONE),
        (400, PushOutcome.FAILED),
        (413, PushOutcome.FAILED),
        (429, PushOutcome.FAILED),
        (500, PushOutcome.FAILED),
        (301, PushOutcome.FAILED),
    ],
)
def test_each_answer_maps_to_one_outcome(status: int, outcome: PushOutcome) -> None:
    push, _ = sender(lambda _request: httpx.Response(status, headers={"Location": ENDPOINT}))

    assert push.send(Browser().target(), b"{}", ttl=TTL, urgency="normal") is outcome


def test_an_unreachable_service_is_a_failure_not_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    push, _ = sender(handler)

    assert push.send(Browser().target(), b"{}", ttl=TTL, urgency="normal") is PushOutcome.FAILED


@pytest.mark.parametrize(
    "target",
    [
        Browser().target("https://169.254.169.254/latest"),
        PushTarget(endpoint=ENDPOINT, p256dh="not base64!", auth="a" * 22),
        PushTarget(endpoint=ENDPOINT, p256dh=_b64(b"\x04" + b"\x00" * 64), auth="a" * 22),
    ],
)
def test_a_bad_row_is_never_sent(target: PushTarget) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201)

    push, _ = sender(handler)

    assert push.send(target, b"{}", ttl=TTL, urgency="normal") is PushOutcome.FAILED
    assert seen == []


def test_the_token_names_the_checked_host_whatever_its_case() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201)

    push, public = sender(handler)
    push.send(
        Browser().target("https://FCM.googleapis.com/fcm/send/x"), b"{}", ttl=TTL, urgency="normal"
    )

    token = seen[0].headers["authorization"].split("t=", 1)[1].split(",", 1)[0]
    point = base64url_bytes(public)
    assert point is not None
    verifying = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
    assert jwt.decode(token, verifying, algorithms=["ES256"], audience="https://fcm.googleapis.com")


def test_push_off_sends_nothing_and_on_builds_the_sender() -> None:
    public, private = vapid_pair()

    off = web_push_for(Settings(_env_file=None))  # type: ignore[call-arg]
    on = web_push_for(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            vapid_public_key=public,
            vapid_private_key=private,
            vapid_subject=SUBJECT,
        )
    )

    assert isinstance(off, NullWebPush)
    assert off.send(Browser().target(), b"{}", ttl=TTL, urgency="normal") is PushOutcome.FAILED
    assert isinstance(on, WebPushSender)
