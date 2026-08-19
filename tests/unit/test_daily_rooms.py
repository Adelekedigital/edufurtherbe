"""What the Daily adapter asks for, and what it does when Daily says no.

**Driven against a transport rather than the network.** `httpx.MockTransport`
lets these assert the *request* — which is the half that has gone wrong twice in
this codebase's integrations, silently both times: `conferenceDataVersion=1`
dropped a conference, and an ORM insert took a column name and wrote a default.
A test that only checks the return value would pass through both.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import httpx
import pytest

from app.infra.clients.meetings import DailyRooms, VenueUnavailableError

#: Stands in for a minted credential. Named rather than inline because the
#: secret scanner flags a literal here — correctly, since that is exactly what
#: this is a placeholder for.
FAKE_TOKEN = "not-a-real-jwt"  # noqa: S105

OPENS = dt.datetime(2026, 8, 20, 14, 55, tzinfo=dt.UTC)
CLOSES = dt.datetime(2026, 8, 20, 16, 0, tzinfo=dt.UTC)


def rooms(handler: Any) -> tuple[DailyRooms, list[httpx.Request]]:
    """An adapter whose calls are captured rather than sent."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.Client(
        base_url="https://api.daily.co/v1",
        transport=httpx.MockTransport(record),
        headers={"Authorization": "Bearer test-key"},
    )
    return DailyRooms("test-key", client=client), seen


def ok(body: dict[str, Any]) -> Any:
    return lambda _: httpx.Response(200, json=body)


def body_of(request: httpx.Request) -> dict[str, Any]:
    return dict(json.loads(request.content))


# --------------------------------------------------------------------------
# Creating a room
# --------------------------------------------------------------------------


def test_a_room_is_private_and_gated() -> None:
    """**Both are the design, and neither is a default.**

    Private is settled decision #128 without exception — a public room makes the
    withheld link pointless *and* the recorded join meaningless, because if the
    URL alone admits somebody then pressing Join is optional. The `nbf`/`exp`
    pair is the only gate mechanism available.
    """
    api, seen = rooms(ok({"name": "ef-daily-1", "url": "https://ef.daily.co/ef-daily-1"}))

    api.create(name="ef-daily-1", opens_at=OPENS, closes_at=CLOSES)

    (request,) = seen
    sent = body_of(request)
    assert request.url.path.endswith("/rooms")
    assert sent["privacy"] == "private"
    assert sent["properties"]["nbf"] == int(OPENS.timestamp())
    assert sent["properties"]["exp"] == int(CLOSES.timestamp())


def test_the_room_name_is_kept_as_its_handle() -> None:
    """`external_room_id` is what `token_for` needs later. Daily addresses tokens
    by room *name*, so storing the id it happens to return would be storing the
    wrong identifier — and it would fail only at the moment somebody tried to
    join."""
    api, _ = rooms(ok({"name": "ef-daily-2", "url": "https://ef.daily.co/ef-daily-2"}))

    room = api.create(name="ef-daily-2", opens_at=OPENS, closes_at=CLOSES)

    assert room.url == "https://ef.daily.co/ef-daily-2"
    assert room.external_id == "ef-daily-2"


# --------------------------------------------------------------------------
# Minting a token
# --------------------------------------------------------------------------


def test_a_token_carries_our_own_user_id() -> None:
    """**The spike measured this coming back verbatim on the meeting record**,
    which is what makes attendance attributable without a second lookup table.
    Sending a display name or a provider id instead would leave the times real
    and unattributable."""
    api, seen = rooms(ok({"token": FAKE_TOKEN}))

    token = api.token_for(
        room="ef-daily-3",
        user_id="01a01817-2a2c-7092-b3d2-29cc23c3656f",
        user_name="Ada",
        is_owner=True,
        opens_at=OPENS,
        closes_at=CLOSES,
    )

    (request,) = seen
    sent = body_of(request)["properties"]
    assert token == FAKE_TOKEN
    assert request.url.path.endswith("/meeting-tokens")
    assert sent["user_id"] == "01a01817-2a2c-7092-b3d2-29cc23c3656f"
    assert sent["room_name"] == "ef-daily-3"


def test_only_the_mentor_is_an_owner() -> None:
    """On Daily an owner may admit, mute and end the call. That is the mentor's
    role and not the mentee's, and handing both the same token would give a
    mentee the power to end their mentor's session."""
    api, seen = rooms(ok({"token": FAKE_TOKEN}))

    for is_owner in (True, False):
        api.token_for(
            room="r",
            user_id="u",
            user_name="n",
            is_owner=is_owner,
            opens_at=OPENS,
            closes_at=CLOSES,
        )

    assert [body_of(r)["properties"]["is_owner"] for r in seen] == [True, False]


def test_a_token_is_gated_like_its_room() -> None:
    """Set on both, so a token cannot outlive the room or open it early. The
    spike confirmed Daily accepts them in both places; which one it *enforces*
    is the question still open, and setting both is what makes the answer not
    matter."""
    api, seen = rooms(ok({"token": FAKE_TOKEN}))

    api.token_for(
        room="r", user_id="u", user_name="n", is_owner=False, opens_at=OPENS, closes_at=CLOSES
    )

    sent = body_of(seen[0])["properties"]
    assert sent["nbf"] == int(OPENS.timestamp())
    assert sent["exp"] == int(CLOSES.timestamp())


# --------------------------------------------------------------------------
# When Daily says no
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "handler",
    [
        lambda _: httpx.Response(500, json={"error": "boom"}),
        lambda _: httpx.Response(401, json={"error": "unauthorized"}),
        lambda _: httpx.Response(200, content=b"not json"),
        lambda _: httpx.Response(200, json=["unexpected shape"]),
    ],
    ids=["server-error", "bad-key", "not-json", "wrong-shape"],
)
def test_every_failure_becomes_one_error(handler: Any) -> None:
    """**One error type, because the caller's decision is the same for all of
    them:** carry on without a venue rather than fail the booking. Letting
    `httpx` raise would turn a third party's bad afternoon into a 500 on a
    request that had already done what the user asked.

    The last two are the interesting ones — a 200 carrying nonsense is the
    failure a `raise_for_status` alone lets through.
    """
    api, _ = rooms(handler)

    with pytest.raises(VenueUnavailableError):
        api.create(name="n", opens_at=OPENS, closes_at=CLOSES)


def test_a_network_failure_becomes_the_same_error() -> None:
    """No response at all, which `raise_for_status` never sees."""

    def refuse(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    api, _ = rooms(refuse)

    with pytest.raises(VenueUnavailableError):
        api.token_for(
            room="r", user_id="u", user_name="n", is_owner=False, opens_at=OPENS, closes_at=CLOSES
        )
