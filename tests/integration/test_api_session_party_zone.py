"""Each party's time zone, and the offering's name, on a session read.

The Bookings design reads "9:00 am to 10:00 am for Amara in Lagos", marked late
when the other person's hour is before 7am or after 10pm — the line that stops
a mentor confirming 2am for somebody. That needs the *other* party's zone, and
`UserRead.timezone` only ever carried the caller's own.

The heading reads "School shortlist session with Amara", which needs the
offering's name rather than its id. It is the same `{id, name}` a review names
its topic with (#185): the offering's **current** name, whatever its state.
"""

from __future__ import annotations

from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from test_api_sessions import delete_account, make_session, pair, sessions_url

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def set_zone(engine: AsyncEngine, user_id: UUID, zone: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET timezone = :z WHERE id = :u"), {"z": zone, "u": user_id}
        )


async def give_session_type(
    engine: AsyncEngine, session_id: UUID, mentor: UUID, name: str, *, deleted: bool = False
) -> UUID:
    async with engine.begin() as conn:
        type_id = (
            await conn.execute(
                text(
                    "INSERT INTO session_types (mentor_user_id, name, deleted_at) "
                    "VALUES (:m, :n, CASE WHEN :d THEN now() END) RETURNING id"
                ),
                {"m": mentor, "n": name, "d": deleted},
            )
        ).scalar_one()
        await conn.execute(
            text("UPDATE sessions SET session_type_id = :t WHERE id = :s"),
            {"t": type_id, "s": session_id},
        )
    return type_id


async def detail(client: httpx.AsyncClient, session_id: UUID, auth: UUID) -> dict[str, object]:
    response = await client.get(f"/api/v1/sessions/{session_id}", headers=bearer(api_token(auth)))
    assert response.status_code == 200
    body: dict[str, object] = response.json()
    return body


async def first_row(client: httpx.AsyncClient, user: UUID, auth: UUID) -> dict[str, object]:
    response = await client.get(sessions_url(user), headers=bearer(api_token(auth)))
    assert response.status_code == 200
    row: dict[str, object] = response.json()["data"][0]
    return row


# --------------------------------------------------------------------------
# The party's zone
# --------------------------------------------------------------------------


@pytest.mark.parametrize("read", ["detail", "list"])
async def test_both_parties_carry_their_own_zone(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, read: str
) -> None:
    """Each side's own zone, not the caller's copied onto both."""
    mentor, mentor_auth, mentee, _ = await pair(db_engine, f"zone-{read}")
    await set_zone(db_engine, mentor, "Europe/London")
    await set_zone(db_engine, mentee, "Africa/Lagos")
    session_id = await make_session(db_engine, mentor, mentee)

    body = (
        await detail(api_client, session_id, mentor_auth)
        if read == "detail"
        else await first_row(api_client, mentor, mentor_auth)
    )

    assert body["mentor"]["timezone"] == "Europe/London"  # type: ignore[index]
    assert body["mentee"]["timezone"] == "Africa/Lagos"  # type: ignore[index]


@pytest.mark.parametrize("read", ["detail", "list"])
async def test_a_deleted_party_has_no_zone(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, read: str
) -> None:
    """Their place stays and their identity goes (#93) — the zone with it, since
    "late for Amara" about somebody who left says something about them."""
    mentor, _, mentee, mentee_auth = await pair(db_engine, f"zone-gone-{read}")
    session_id = await make_session(db_engine, mentor, mentee)
    await delete_account(db_engine, mentor)

    body = (
        await detail(api_client, session_id, mentee_auth)
        if read == "detail"
        else await first_row(api_client, mentee, mentee_auth)
    )

    assert body["mentor"]["deleted"] is True  # type: ignore[index]
    assert body["mentor"]["timezone"] is None  # type: ignore[index]
    assert body["mentee"]["timezone"] == "Africa/Lagos"  # type: ignore[index]


async def test_party_keys_are_exactly_these(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A field added to a party is a deliberate act: what one person learns
    about the other through a session they share."""
    mentor, mentor_auth, mentee, _ = await pair(db_engine, "zone-keys")
    session_id = await make_session(db_engine, mentor, mentee)

    body = await detail(api_client, session_id, mentor_auth)

    assert set(body["mentee"]) == {  # type: ignore[arg-type]
        "id",
        "deleted",
        "first_name",
        "last_name",
        "avatar_url",
        "avatar_focus",
        "timezone",
        "joined_at",
        # #382: when Daily first saw this party in the room. Optional in the
        # spec; `joined_at` stays the Join press.
        "in_room_at",
        "attendance_status",
        # The pending card's "BSc Student at FUTA": `top_qualification`.
        "degree",
        "institution",
    }


# --------------------------------------------------------------------------
# The offering's name
# --------------------------------------------------------------------------


@pytest.mark.parametrize("read", ["detail", "list"])
async def test_the_session_names_its_offering(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, read: str
) -> None:
    mentor, _, mentee, mentee_auth = await pair(db_engine, f"type-{read}")
    session_id = await make_session(db_engine, mentor, mentee)
    type_id = await give_session_type(db_engine, session_id, mentor, "School shortlist")

    body = (
        await detail(api_client, session_id, mentee_auth)
        if read == "detail"
        else await first_row(api_client, mentee, mentee_auth)
    )

    assert body["session_type"] == {"id": str(type_id), "name": "School shortlist"}
    assert body["session_type_id"] == str(type_id)


async def test_a_retired_offering_keeps_its_name(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """#185: the offering's current name, whatever its state. Retiring an
    offering does not change what an old session was about."""
    mentor, _, mentee, mentee_auth = await pair(db_engine, "type-retired")
    session_id = await make_session(db_engine, mentor, mentee)
    type_id = await give_session_type(db_engine, session_id, mentor, "Old offer", deleted=True)

    body = await detail(api_client, session_id, mentee_auth)

    assert body["session_type"] == {"id": str(type_id), "name": "Old offer"}


async def test_a_session_with_no_offering_has_none(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Migrated rows predate offerings."""
    mentor, _, mentee, mentee_auth = await pair(db_engine, "type-none")
    session_id = await make_session(db_engine, mentor, mentee)

    body = await detail(api_client, session_id, mentee_auth)

    assert body["session_type"] is None
    assert body["session_type_id"] is None


# --------------------------------------------------------------------------
# Who each party is (the pending card's "BSc Student at FUTA")
# --------------------------------------------------------------------------


async def add_degree(engine: AsyncEngine, user: UUID, degree: str, school: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO education_entries (user_id, school_name_raw, degree_abbreviation) "
                "VALUES (:u, :s, :d)"
            ),
            {"u": user, "s": school, "d": degree},
        )


@pytest.mark.parametrize("read", ["detail", "list"])
async def test_each_party_is_described_by_their_top_qualification(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, read: str
) -> None:
    """**What a mentor reads before accepting**: the mentee's degree and
    institution, chosen by the rule that describes a mentor on the discovery
    card and a reviewer on a review (`top_qualification`). A party with no
    education has neither."""
    mentor, mentor_auth, mentee, _ = await pair(db_engine, f"zone-degree-{read}")
    await add_degree(db_engine, mentee, "BSc", "FUTA")
    session_id = await make_session(db_engine, mentor, mentee)

    body = (
        await detail(api_client, session_id, mentor_auth)
        if read == "detail"
        else await first_row(api_client, mentor, mentor_auth)
    )

    assert body["mentee"]["degree"] == "BSc"  # type: ignore[index]
    assert body["mentee"]["institution"] == "FUTA"  # type: ignore[index]
    assert body["mentor"]["degree"] is None  # type: ignore[index]
    assert body["mentor"]["institution"] is None  # type: ignore[index]


async def test_a_deleted_party_has_no_qualification(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Gone with their name (decision #93): the session stays, the person goes."""
    mentor, mentor_auth, mentee, _ = await pair(db_engine, "zone-degree-gone")
    await add_degree(db_engine, mentee, "BSc", "FUTA")
    session_id = await make_session(db_engine, mentor, mentee)
    await delete_account(db_engine, mentee)

    body = await detail(api_client, session_id, mentor_auth)

    assert body["mentee"]["degree"] is None  # type: ignore[index]
    assert body["mentee"]["institution"] is None  # type: ignore[index]
