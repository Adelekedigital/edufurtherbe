"""What QStash is asked for, and what it takes to be believed on the way back.

**The verification tests are the point of this file.** The callback endpoint
queues messages and takes no bearer token, so its signature check is the only
thing between a stranger and a send-anything primitive — and every way that
check can be *almost* right has its own case here.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
from typing import Any

import httpx
import jwt
import pytest

from app.core.config import QSTASH_EU
from app.domain.notifications import REMINDER_OFFSETS, reminders_for
from app.infra.clients.scheduler import (
    NullScheduler,
    QStashScheduler,
    SchedulerError,
    UntrustedCallbackError,
    verify_callback,
)

URL = "https://api.example.test/api/v1/callbacks/reminders"

#: Stand-ins for QStash's rotating pair. Named so the secret scanner has one
#: obvious placeholder rather than several literals to object to.
CURRENT_KEY = "not-a-real-current-signing-key-for-local-tests"
NEXT_KEY = "not-a-real-next-signing-key-for-local-tests"

DEADLINE = dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.UTC)


def signed(
    body: bytes,
    *,
    key: str = CURRENT_KEY,
    sub: str = URL,
    body_hash: str | None = None,
) -> str:
    """A token shaped like QStash's, with each part overridable so a test can
    break exactly one thing."""
    digest = base64.urlsafe_b64encode(hashlib.sha256(body).digest()).decode()
    now = dt.datetime.now(dt.UTC)
    return jwt.encode(
        {
            "iss": "Upstash",
            "sub": sub,
            "exp": int((now + dt.timedelta(minutes=5)).timestamp()),
            "nbf": int(now.timestamp()),
            "iat": int(now.timestamp()),
            "jti": "one",
            "body": digest if body_hash is None else body_hash,
        },
        key,
        algorithm="HS256",
    )


BODY = json.dumps({"session_id": "abc", "kind": "t12"}).encode()


# --------------------------------------------------------------------------
# Scheduling
# --------------------------------------------------------------------------


def scheduler(handler: Any, *, url: str = QSTASH_EU) -> tuple[QStashScheduler, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return QStashScheduler(
        "token", url, client=httpx.Client(transport=httpx.MockTransport(record))
    ), seen


def test_a_callback_is_published_for_an_instant_not_a_delay() -> None:
    """**A delay is computed from *now*** and drifts by however long the request
    took to reach QStash. The product specifies an instant — twelve hours before
    a deadline — and an instant survives a retry of the publish itself."""
    api, seen = scheduler(lambda _: httpx.Response(200, json={"messageId": "m1"}))
    at = dt.datetime(2026, 9, 1, 0, 0, tzinfo=dt.UTC)

    api.schedule(url=URL, body={"session_id": "abc", "kind": "t12"}, at=at)

    (request,) = seen
    assert request.headers["Upstash-Not-Before"] == str(int(at.timestamp()))
    assert URL in str(request.url)
    assert json.loads(request.content)["kind"] == "t12"


def test_a_refusal_is_raised_so_the_caller_can_decide() -> None:
    """The booking swallows it — a session that exists without a reminder is
    recoverable, where a booking refused because a scheduler was slow is not."""
    api, _ = scheduler(lambda _: httpx.Response(500, json={"error": "boom"}))

    with pytest.raises(SchedulerError):
        api.schedule(url=URL, body={}, at=dt.datetime.now(dt.UTC))


def test_the_default_scheduler_schedules_nothing_and_raises_nothing() -> None:
    """The state of a deployment with no token: bookings work, deadlines pass,
    the sweep still frees the slot, and nobody is nudged."""
    assert NullScheduler().schedule(url=URL, body={}, at=dt.datetime.now(dt.UTC)) is None


# --------------------------------------------------------------------------
# The schedule itself
# --------------------------------------------------------------------------


def test_only_reminders_still_ahead_are_scheduled() -> None:
    """**At the 24-hour booking floor the deadline is eighteen hours away**, so
    `t24` is already behind — firing it immediately would be the booking message
    again, thirty seconds later, saying the same thing."""
    booked_at = DEADLINE - dt.timedelta(hours=18)

    kinds = [kind for kind, _ in reminders_for(DEADLINE, now=booked_at)]

    assert kinds == ["t12"]


def test_a_long_lead_gets_both() -> None:
    booked_at = DEADLINE - dt.timedelta(days=3)

    schedule = dict(reminders_for(DEADLINE, now=booked_at))

    assert set(schedule) == set(REMINDER_OFFSETS)
    assert schedule["t24"] == DEADLINE - dt.timedelta(hours=24)
    assert schedule["t12"] == DEADLINE - dt.timedelta(hours=12)


def test_a_deadline_already_past_schedules_nothing() -> None:
    """Unreachable through the API, and the right answer if reached: a reminder
    about a deadline that has gone is a message with nothing to ask for."""
    assert reminders_for(DEADLINE, now=DEADLINE + dt.timedelta(hours=1)) == ()


# --------------------------------------------------------------------------
# Believing the callback
# --------------------------------------------------------------------------


def test_a_properly_signed_callback_is_accepted() -> None:
    assert (
        verify_callback(token=signed(BODY), body=BODY, url=URL, signing_keys=(CURRENT_KEY,)) is None
    )


def test_a_body_that_does_not_match_its_signature_is_refused() -> None:
    """**The load-bearing check.** A valid signature proves QStash issued *a*
    token; the hash proves it issued one for *this payload*. Without it, anybody
    who observed one callback could replay its signature against a body of their
    choosing — and this endpoint queues messages."""
    tampered = json.dumps({"session_id": "someone-elses", "kind": "t12"}).encode()

    with pytest.raises(UntrustedCallbackError) as raised:
        verify_callback(token=signed(BODY), body=tampered, url=URL, signing_keys=(CURRENT_KEY,))

    assert "body" in str(raised.value)


def test_a_token_minted_for_another_endpoint_is_refused() -> None:
    """`sub` names the destination, so a token for a different callback of ours
    cannot be replayed at this one."""
    with pytest.raises(UntrustedCallbackError):
        verify_callback(
            token=signed(BODY, sub="https://api.example.test/api/v1/callbacks/other"),
            body=BODY,
            url=URL,
            signing_keys=(CURRENT_KEY,),
        )


def test_an_unknown_key_is_refused() -> None:
    with pytest.raises(UntrustedCallbackError):
        verify_callback(
            token=signed(BODY, key="somebody-elses-signing-key-for-local-tests"),
            body=BODY,
            url=URL,
            signing_keys=(CURRENT_KEY,),
        )


def test_either_key_in_a_rotation_is_accepted() -> None:
    """**Both are tried, so a rotation does not drop callbacks** in the window
    where the old and the new are each live. Asserted from both sides, because a
    version checking only the first would pass on whichever half a test
    happened to use."""
    for key in (CURRENT_KEY, NEXT_KEY):
        assert (
            verify_callback(
                token=signed(BODY, key=key),
                body=BODY,
                url=URL,
                signing_keys=(CURRENT_KEY, NEXT_KEY),
            )
            is None
        )


def test_an_expired_token_is_refused() -> None:
    """QStash's tokens live five minutes. One older than that is a replay of
    something observed earlier."""
    now = dt.datetime.now(dt.UTC)
    stale = jwt.encode(
        {
            "iss": "Upstash",
            "sub": URL,
            "exp": int((now - dt.timedelta(minutes=1)).timestamp()),
            "nbf": int((now - dt.timedelta(minutes=10)).timestamp()),
            "body": base64.urlsafe_b64encode(hashlib.sha256(BODY).digest()).decode(),
        },
        CURRENT_KEY,
        algorithm="HS256",
    )

    with pytest.raises(UntrustedCallbackError):
        verify_callback(token=stale, body=BODY, url=URL, signing_keys=(CURRENT_KEY,))


def test_a_token_from_another_issuer_is_refused() -> None:
    """Anybody can mint an HS256 token. Only QStash mints one claiming to be
    Upstash *and* signed with a key we hold."""
    now = dt.datetime.now(dt.UTC)
    forged = jwt.encode(
        {
            "iss": "NotUpstash",
            "sub": URL,
            "exp": int((now + dt.timedelta(minutes=5)).timestamp()),
            "nbf": int(now.timestamp()),
            "body": base64.urlsafe_b64encode(hashlib.sha256(BODY).digest()).decode(),
        },
        CURRENT_KEY,
        algorithm="HS256",
    )

    with pytest.raises(UntrustedCallbackError):
        verify_callback(token=forged, body=BODY, url=URL, signing_keys=(CURRENT_KEY,))


def test_an_absent_signature_is_refused() -> None:
    with pytest.raises(UntrustedCallbackError):
        verify_callback(token="", body=BODY, url=URL, signing_keys=(CURRENT_KEY,))


def test_no_configured_key_accepts_nothing() -> None:
    """**An unconfigured verifier must reject rather than wave through.** On a
    public endpoint that queues messages, one that rejects everything is visibly
    broken; one that accepts everything is quietly open."""
    with pytest.raises(UntrustedCallbackError):
        verify_callback(token=signed(BODY), body=BODY, url=URL, signing_keys=())


def test_the_configured_region_is_where_the_callback_is_published() -> None:
    """**QStash is region-scoped and `qstash.upstash.io` is the EU, not a global
    endpoint.** A token issued in `us-east-1` answers the EU host with `404`
    naming a region — not `401` — so a wrong endpoint reads as a broken path and
    sends whoever debugs it after the URL rather than the configuration.

    The host was a module constant with nothing asserting it, which is why it
    survived. This is that assertion: the origin the caller configured is the
    origin the request goes to.
    """
    api, seen = scheduler(
        lambda _: httpx.Response(200, json={"messageId": "m1"}),
        url="https://qstash-us-east-1.upstash.io",
    )

    api.schedule(url=URL, body={"session_id": "abc"}, at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC))

    (request,) = seen
    assert str(request.url).startswith("https://qstash-us-east-1.upstash.io/v2/publish/")
    # The destination survives being nested in the path, which is the shape a
    # refactor to `base_url` plus a relative path would quietly mangle.
    assert URL in str(request.url)


def test_a_trailing_slash_on_the_origin_does_not_double_up() -> None:
    """`https://host/` and `https://host` must publish to the same place.

    A doubled slash is a `404` from QStash, which is the same symptom as a wrong
    region — and an operator pasting a URL from a console is exactly where the
    trailing slash comes from.
    """
    api, seen = scheduler(
        lambda _: httpx.Response(200, json={"messageId": "m1"}),
        url="https://qstash-us-east-1.upstash.io/",
    )

    api.schedule(url=URL, body={}, at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC))

    (request,) = seen
    assert "//v2/publish" not in str(request.url)
    assert str(request.url).startswith("https://qstash-us-east-1.upstash.io/v2/publish/")


def test_a_refused_publish_carries_what_qstash_said() -> None:
    """**Without this, deleting the detail leaves the suite green.**

    `SchedulerError` is caught and logged at INFO by both callers, so a publish
    that fails explains itself only in that log line. If the upstream body stops
    travelling with the error, the regression is invisible in the tests *and* in
    production — which is how the bare `404` this whole change is about survived
    in the first place.
    """
    body = '{"error":"user (abc) not found in this region (eu-central-1)."}'
    api, _ = scheduler(lambda _: httpx.Response(404, content=body))

    with pytest.raises(SchedulerError) as raised:
        api.schedule(url=URL, body={}, at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC))

    assert "not found in this region" in str(raised.value)
    assert "eu-central-1" in str(raised.value)
