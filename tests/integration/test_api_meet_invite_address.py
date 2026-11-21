"""Meet invites go to the Google account, and each party is told which address
was invited (decision 357).

What carries weight: a mentee who signed up with a Gmail "+" alias is invited
as the plain address (or Meet keeps them waiting to be admitted), and
`meet_invite_email` shows each viewer their own invited address on a Meet
session, and nobody else's, and nothing on another venue.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.meeting_fakes import FAKE_MEET, FakeCalendar, FakeRooms
from tests.integration.test_api_meeting_provisioning import (
    a_mentor_on,
    book,
    fakes,
    joinable,
)

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

__all__ = ["fakes"]

ALIAS = "ada.obi+edu-{tag}@gmail.com"
ACCOUNT = "ada.obi@gmail.com"


async def mentor_email(engine: AsyncEngine, mentor: object) -> str:
    async with engine.connect() as conn:
        return str(
            (
                await conn.execute(text("SELECT email FROM users WHERE id = :u"), {"u": mentor})
            ).scalar_one()
        )


async def test_a_gmail_alias_is_invited_as_the_account(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    fakes: tuple[FakeRooms, FakeCalendar],
) -> None:
    _, calendar = fakes
    setup = await a_mentor_on(
        db_engine, "mia-alias", "google_meet", mentee_email=ALIAS.format(tag="a")
    )

    await book(api_client, setup)

    (event,) = calendar.calls
    assert event["attendee_emails"][0] == ACCOUNT
    assert event["attendee_emails"][1] == await mentor_email(db_engine, setup["mentor"])


@pytest.mark.usefixtures("fakes")
async def test_each_party_sees_their_own_invited_address_on_a_meet_session(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    setup = await a_mentor_on(
        db_engine, "mia-own", "google_meet", mentee_email=ALIAS.format(tag="b")
    )
    created = await book(api_client, setup)
    assert created["meet_invite_email"] == ACCOUNT

    as_mentee = await api_client.get(
        f"/api/v1/sessions/{created['id']}", headers=setup["mentee_headers"]
    )
    as_mentor = await api_client.get(
        f"/api/v1/sessions/{created['id']}", headers=setup["mentor_headers"]
    )
    listed = await api_client.get(
        f"/api/v1/users/{setup['mentor']}/sessions", headers=setup["mentor_headers"]
    )

    assert as_mentee.json()["meet_invite_email"] == ACCOUNT
    mentor_address = await mentor_email(db_engine, setup["mentor"])
    assert as_mentor.json()["meet_invite_email"] == mentor_address
    (row,) = [s for s in listed.json()["data"] if s["id"] == created["id"]]
    assert row["meet_invite_email"] == mentor_address


@pytest.mark.usefixtures("fakes")
@pytest.mark.parametrize("provider", ["daily", "custom"])
async def test_another_venue_carries_no_invited_address(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, provider: str
) -> None:
    setup = await a_mentor_on(
        db_engine, f"mia-{provider}", provider, mentee_email=ALIAS.format(tag=provider)
    )
    created = await book(api_client, setup)

    shown = await api_client.get(
        f"/api/v1/sessions/{created['id']}", headers=setup["mentee_headers"]
    )

    assert shown.json()["meet_invite_email"] is None


@pytest.mark.usefixtures("door")
@pytest.mark.parametrize("path", ["join", "door"])
async def test_a_gmail_party_is_handed_the_meet_under_their_invited_account(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, path: str
) -> None:
    """A rejoin from a browser already on Meet opens under the invited
    account rather than the browser's default one."""
    setup = await a_mentor_on(
        db_engine, f"mia-{path}", "google_meet", mentee_email=ALIAS.format(tag=path)
    )
    session = await joinable(db_engine, api_client, setup)

    entered = await api_client.post(
        f"/api/v1/sessions/{session['id']}/{path}", headers=setup["mentee_headers"]
    )

    assert entered.status_code == 200, entered.text
    assert entered.json()["meeting_url"] == f"{FAKE_MEET}?authuser=ada.obi%40gmail.com"
    async with db_engine.connect() as conn:
        stored = await conn.execute(
            text("SELECT meeting_url FROM sessions WHERE id = :i"), {"i": session["id"]}
        )
        assert stored.scalar_one() == FAKE_MEET
