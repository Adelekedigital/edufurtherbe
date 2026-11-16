"""`POST /mentors/{handle}/visits` and `/shares` (decisions 346, 347).

Public writes that answer `204` whether or not a row was stored: a visit is
recorded once per visitor per mentor per UTC day, never the mentor's own; a
share once per person per method per day. The handle resolves exactly as the
profile read resolves it, so the same handles are `404`.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import admin_token, make_public_mentor, token_for
from tests.integration.test_api_booking import a_mentee

from app.domain.rate_limits import SlidingWindow
from conftest import bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]

BROWSER = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 14) Chrome/131",
    "X-Forwarded-For": "203.0.113.7",
}


def visits(handle: object) -> str:
    return f"/api/v1/mentors/{handle}/visits"


def shares(handle: object) -> str:
    return f"/api/v1/mentors/{handle}/shares"


async def rows(engine: AsyncEngine, table: str) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        result = await conn.execute(text(f"SELECT * FROM {table} ORDER BY created_at"))  # noqa: S608
        return [dict(row) for row in result.mappings()]


async def test_a_guest_visit_is_recorded_with_its_channel_and_referrer(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "visited")

    response = await api_client.post(
        visits(mentor),
        json={"via": "copy", "referrer_host": "WWW.LinkedIn.com:443"},
        headers=BROWSER,
    )

    assert response.status_code == 204
    assert response.content == b""
    [row] = await rows(db_engine, "mentor_profile_visits")
    assert row["mentor_user_id"] == mentor
    assert row["via"] == "copy"
    assert row["referrer_host"] == "linkedin.com"
    assert row["signed_in"] is False
    assert len(row["visitor_key"]) == 32
    # Nothing about the visitor is stored in the clear.
    assert b"203.0.113.7" not in bytes(row["visitor_key"])


async def test_a_visitor_counts_once_a_day_and_keeps_the_first_visits_hints(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "repeat", slug="ada-repeat")

    for body in ({"via": "share", "referrer_host": "wa.me"}, {"via": "copy"}, {}):
        assert (
            await api_client.post(visits(mentor), json=body, headers=BROWSER)
        ).status_code == 204
    # The slug is the same mentor, and the same visitor.
    assert (
        await api_client.post(visits("ada-repeat"), json={}, headers=BROWSER)
    ).status_code == 204

    [row] = await rows(db_engine, "mentor_profile_visits")
    assert (row["via"], row["referrer_host"]) == ("share", "wa.me")

    # Another browser behind the same address is another visitor.
    other = BROWSER | {"User-Agent": "Mozilla/5.0 (iPhone) Safari/605"}
    assert (await api_client.post(visits(mentor), json={}, headers=other)).status_code == 204
    assert len(await rows(db_engine, "mentor_profile_visits")) == 2


async def test_a_signed_in_visitor_is_one_visitor_wherever_they_are(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "member-visit")
    _, headers = await a_mentee(db_engine, "member-visitor")

    for address in ("203.0.113.7", "198.51.100.9"):
        sent = headers | {"X-Forwarded-For": address}
        assert (await api_client.post(visits(mentor), json={}, headers=sent)).status_code == 204

    [row] = await rows(db_engine, "mentor_profile_visits")
    assert row["signed_in"] is True


async def test_unknown_and_odd_hints_are_stored_as_null_not_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "odd")

    response = await api_client.post(
        visits(mentor),
        json={"via": "newsletter", "referrer_host": "a" * 300 + ".com"},
        headers=BROWSER,
    )

    assert response.status_code == 204
    [row] = await rows(db_engine, "mentor_profile_visits")
    assert (row["via"], row["referrer_host"]) == (None, None)


async def test_a_mentor_viewing_their_own_profile_is_not_a_visit(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "self-view")
    own = bearer(await token_for(db_engine, mentor))

    response = await api_client.post(visits(mentor), json={"via": "copy"}, headers=own)

    assert response.status_code == 204
    assert await rows(db_engine, "mentor_profile_visits") == []


@pytest.mark.parametrize("knob", [{"listed": False}, {"approved": False}])
async def test_a_mentor_the_public_cannot_see_is_404_as_on_the_read(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, knob: dict[str, bool]
) -> None:
    hidden = await make_public_mentor(db_engine, "hidden", **knob)

    for url in (visits(hidden), shares(hidden)):
        body = {"method": "copy"} if url.endswith("shares") else {}
        assert (await api_client.post(url, json=body)).status_code == 404
    assert (await api_client.get(f"/api/v1/mentors/{hidden}")).status_code == 404
    assert (await api_client.post(visits("nobody-at-all"), json={})).status_code == 404

    # Its owner may read it, so their own look is a 204 that records nothing.
    own = bearer(await token_for(db_engine, hidden))
    assert (await api_client.post(visits(hidden), json={}, headers=own)).status_code == 204
    assert (
        await api_client.post(shares(hidden), json={"method": "copy"}, headers=own)
    ).status_code == 204
    assert await rows(db_engine, "mentor_profile_visits") == []
    assert await rows(db_engine, "mentor_profile_shares") == []


async def test_a_share_is_recorded_and_own_comes_from_the_token(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "shared")
    own = bearer(await token_for(db_engine, mentor))

    guest = await api_client.post(
        shares(mentor), json={"method": "native", "own": True}, headers=BROWSER
    )
    mine = await api_client.post(shares(mentor), json={"method": "copy", "own": False}, headers=own)
    again = await api_client.post(shares(mentor), json={"method": "copy"}, headers=own)

    assert (guest.status_code, mine.status_code, again.status_code) == (204, 204, 204)
    found = await rows(db_engine, "mentor_profile_shares")
    assert [(r["method"], r["own"], r["signed_in"]) for r in found] == [
        ("native", False, False),
        ("copy", True, True),
    ]
    assert all(r["mentor_user_id"] == mentor for r in found)


async def test_a_share_needs_a_known_method(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "bad-share")

    for body in ({"method": "email"}, {}, {"method": "copy", "extra": 1}):
        assert (await api_client.post(shares(mentor), json=body)).status_code == 422
    assert await rows(db_engine, "mentor_profile_shares") == []


async def test_past_the_budget_events_are_dropped_not_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    mentor = await make_public_mentor(db_engine, "flood")
    app: Any = api_client._transport.app  # type: ignore[attr-defined]
    app.state.link_events_budget = SlidingWindow(1, window_seconds=60.0)

    first = await api_client.post(visits(mentor), json={}, headers=BROWSER)
    other = BROWSER | {"User-Agent": "another browser"}
    second = await api_client.post(visits(mentor), json={}, headers=other)
    third = await api_client.post(shares(mentor), json={"method": "copy"}, headers=other)

    assert (first.status_code, second.status_code, third.status_code) == (204, 204, 204)
    assert len(await rows(db_engine, "mentor_profile_visits")) == 1
    assert await rows(db_engine, "mentor_profile_shares") == []
    assert [r.message for r in caplog.records].count(
        "link events over budget; dropping until it frees"
    ) == 1


async def test_a_visitor_key_differs_by_mentor(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """One visitor's rows for two mentors cannot be joined into a trail."""
    first = await make_public_mentor(db_engine, "trail-a")
    second = await make_public_mentor(db_engine, "trail-b")

    for mentor in (first, second):
        await api_client.post(visits(mentor), json={}, headers=BROWSER)

    keys = {bytes(r["visitor_key"]) for r in await rows(db_engine, "mentor_profile_visits")}
    assert len(keys) == 2


async def test_a_hidden_mentor_is_404_to_a_signed_in_stranger_and_silent_for_an_admin(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    hidden = await make_public_mentor(db_engine, "hidden-signed", listed=False)
    _, stranger = await a_mentee(db_engine, "hidden-stranger")
    admin = bearer(await admin_token(db_engine))

    assert (await api_client.post(visits(hidden), json={}, headers=stranger)).status_code == 404
    # The admin may read it (decision 297), so it is a 204 that stores nothing.
    assert (await api_client.post(visits(hidden), json={}, headers=admin)).status_code == 204
    assert (
        await api_client.post(shares(hidden), json={"method": "copy"}, headers=admin)
    ).status_code == 204
    assert await rows(db_engine, "mentor_profile_visits") == []
    assert await rows(db_engine, "mentor_profile_shares") == []


async def test_a_visit_body_out_of_shape_is_422(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "bad-visit")

    for body in (
        {"via": "c" * 2049},
        {"referrer_host": "h" * 2049},
        {"via": 7},
        {"via": "copy", "extra": 1},
    ):
        assert (await api_client.post(visits(mentor), json=body)).status_code == 422, body
    # The longest accepted hint is dropped, not refused.
    assert (await api_client.post(visits(mentor), json={"via": "c" * 2048})).status_code == 204
    [row] = await rows(db_engine, "mentor_profile_visits")
    assert row["via"] is None
