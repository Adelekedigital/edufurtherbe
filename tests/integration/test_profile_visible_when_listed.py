"""An approved, listed mentor is visible whether or not they can be booked.

Owner, 2026-09-29, amending settled decision #192: hiding a session type
affects only that type. A mentor with no active offering, or no weekly hours,
stays on Explore and keeps a public profile — with `taking_bookings: false` so
the page can say "Not taking bookings". Booking itself is unchanged: nothing is
offered that cannot be booked. Pending and unlisted mentors are still hidden.
The alternative "away" flow is #297; revisiting this rule is #298.
"""

from __future__ import annotations

from uuid import UUID

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import (
    add_availability,
    add_session_type,
    make_bookable_mentor,
    make_public_mentor,
)
from tests.integration.test_mentor_owner_view import a_stranger, auth_of

from app.api.schemas.common import MAX_PAGE_SIZE

pytestmark = [pytest.mark.db, pytest.mark.anyio]


def url(handle: object, tail: str = "") -> str:
    return f"/api/v1/mentors/{handle}{tail}"


async def on_explore(client: httpx.AsyncClient, mentor: UUID) -> dict[str, object] | None:
    """The mentor's Explore card, paging through the whole list."""
    cursor: str | None = None
    while True:
        query = f"?limit={MAX_PAGE_SIZE}" + (f"&cursor={cursor}" if cursor else "")
        page = (await client.get(f"/api/v1/mentors{query}")).json()
        for card in page["data"]:
            if card["id"] == str(mentor):
                return dict(card)
        cursor = page.get("next_cursor")
        if not cursor:
            return None


async def test_a_mentor_with_no_hours_is_visible_but_not_taking_bookings(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "no-hours")
    await add_session_type(db_engine, mentor)

    anonymous = await api_client.get(url(mentor))
    signed_in = await api_client.get(url(mentor), headers=await a_stranger(db_engine, "no-hours"))

    assert anonymous.status_code == signed_in.status_code == 200
    assert anonymous.json()["taking_bookings"] is False
    assert anonymous.json()["next_available_state"] == "none"
    assert anonymous.json()["next_available_at"] is None


async def test_a_mentor_who_hid_their_last_offering_stays_visible(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "hid-last")
    await add_availability(db_engine, mentor)
    await add_session_type(db_engine, mentor, active=False)

    response = await api_client.get(url(mentor))

    assert response.status_code == 200
    assert response.json()["taking_bookings"] is False
    assert response.json()["session_types"] == []


async def test_a_bookable_mentor_is_taking_bookings(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "live")

    response = await api_client.get(url(mentor))

    assert response.status_code == 200
    assert response.json()["taking_bookings"] is True
    assert "setup_needed" not in response.json()


async def test_explore_lists_a_mentor_who_is_not_taking_bookings(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    idle = await make_public_mentor(db_engine, "explore-idle")
    await add_session_type(db_engine, idle)
    bookable = await make_bookable_mentor(db_engine, "explore-live")

    idle_card = await on_explore(api_client, idle)
    live_card = await on_explore(api_client, bookable)

    assert idle_card is not None, "a listed mentor vanished from Explore"
    assert idle_card["taking_bookings"] is False
    assert idle_card["next_available_state"] == "none"
    assert live_card is not None and live_card["taking_bookings"] is True


@pytest.mark.parametrize(("approved", "listed"), [(False, True), (True, False)])
async def test_an_unapproved_or_unlisted_mentor_is_still_hidden(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, approved: bool, listed: bool
) -> None:
    mentor = await make_bookable_mentor(
        db_engine, f"hidden-{approved}-{listed}".lower(), approved=approved, listed=listed
    )

    assert (await api_client.get(url(mentor))).status_code == 404
    assert await on_explore(api_client, mentor) is None


async def test_a_hidden_offering_still_cannot_be_booked(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Visibility widened; bookability did not. The profile is public, the
    switched-off offering is still not."""
    mentor = await make_public_mentor(db_engine, "hidden-offering")
    await add_availability(db_engine, mentor)
    hidden = await add_session_type(db_engine, mentor, active=False)

    slots = await api_client.get(
        f"/api/v1/users/{mentor}/availability/slots?session_type_id={hidden}"
    )

    assert (await api_client.get(url(mentor))).status_code == 200
    assert slots.status_code == 404


@pytest.mark.parametrize(
    ("offering", "hours", "needed"),
    [
        (True, False, ["weekly_hours"]),
        (False, True, ["session_type"]),
        (False, False, ["session_type", "weekly_hours"]),
        (True, True, []),
    ],
)
async def test_the_owner_is_told_what_is_missing(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    offering: bool,
    hours: bool,
    needed: list[str],
) -> None:
    mentor = await make_public_mentor(db_engine, f"setup-{offering}-{hours}".lower())
    if offering:
        await add_session_type(db_engine, mentor)
    if hours:
        await add_availability(db_engine, mentor)

    response = await api_client.get(url(mentor), headers=await auth_of(db_engine, mentor))

    assert response.status_code == 200
    assert response.json()["setup_needed"] == needed
    assert response.json()["taking_bookings"] is (offering and hours)


async def test_the_reviews_of_a_visible_mentor_are_public(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "no-hours-reviews")
    await add_session_type(db_engine, mentor)

    anonymous = await api_client.get(url(mentor, "/reviews"))

    assert anonymous.status_code == 200


@pytest.mark.parametrize(("approved", "listed"), [(False, True), (True, False)])
async def test_a_hidden_owner_is_not_taking_bookings_even_when_set_up(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, approved: bool, listed: bool
) -> None:
    """`taking_bookings` means someone can book now. A pending or unlisted owner
    with an offering and hours reads their own profile, and nobody can book them:
    `/slots` is a 404, so the flag is `false` beside `next_available_state: none`."""
    mentor = await make_bookable_mentor(
        db_engine, f"owner-hidden-{approved}-{listed}".lower(), approved=approved, listed=listed
    )

    response = await api_client.get(url(mentor), headers=await auth_of(db_engine, mentor))

    assert response.status_code == 200
    assert response.json()["taking_bookings"] is False
    assert response.json()["next_available_state"] == "none"
