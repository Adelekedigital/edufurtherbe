"""D88's reader step: the owner profile reads its booking settings from the
primary offering, and the mentor-level write reaches them.

**The write half is why this file exists.** The expand step dual-wrote from the
ETL and not from `profile_writer`, so `PATCH /mentor-profile` set
`requires_booking_confirmation` on `mentor_profiles` alone. That was harmless
while `mentor_profiles` was still authoritative and becomes silent data loss the
moment the readers move — the mentor toggles the setting, is answered 200, and
nothing they or anyone else reads changes. No existing test could see it: the
write suite asserts the response, and the read suite seeds rows directly.

The null cases are not an edge. `trg_refuse_retiring_a_primary_offering` refuses
to retire an offering something points at, so *release the pointer, then retire*
is the sanctioned two-step and "live offerings, no primary" is a state mentors
pass through by design.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import add_session_type, make_public_mentor

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def as_mentor(engine: AsyncEngine, tag: str, **kwargs: object) -> tuple[UUID, UUID]:
    """A public mentor who can also authenticate. Returns `(user_id, auth_id)`.

    `make_public_mentor` writes no `auth_id`, because every reader that uses it
    is unauthenticated. The write half here needs one.
    """
    mentor = await make_public_mentor(engine, tag, **kwargs)  # type: ignore[arg-type]
    auth_id = uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET auth_id = :a WHERE id = :u"), {"a": auth_id, "u": mentor}
        )
    return mentor, auth_id


async def make_primary(engine: AsyncEngine, mentor: UUID, session_type: UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE mentor_profiles SET primary_session_type_id = :t WHERE user_id = :u"),
            {"t": session_type, "u": mentor},
        )


async def release_primary(engine: AsyncEngine, mentor: UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE mentor_profiles SET primary_session_type_id = NULL WHERE user_id = :u"),
            {"u": mentor},
        )


async def set_confirmation(engine: AsyncEngine, session_type: UUID, value: bool) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE session_type_booking_configs SET requires_booking_confirmation = :v "
                "WHERE session_type_id = :t"
            ),
            {"v": value, "t": session_type},
        )


async def confirmations(engine: AsyncEngine, mentor: UUID) -> dict[str, bool]:
    """Every offering this mentor has, by name, with its confirmation setting."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT st.name, c.requires_booking_confirmation "
                "  FROM session_types st "
                "  JOIN session_type_booking_configs c ON c.session_type_id = st.id "
                " WHERE st.mentor_user_id = :u"
            ),
            {"u": mentor},
        )
        # `.all()` first: `dict()` on a CursorResult takes the mapping path,
        # because it has `.keys()`, and then fails subscripting it. Ruff's
        # C416 autofix rewrote the comprehension into exactly that.
        return dict(rows.all())


def profile_url(user_id: UUID) -> str:
    return f"/api/v1/users/{user_id}/mentor-profile"


async def read_profile(
    client: httpx.AsyncClient, user_id: UUID, auth_id: UUID
) -> dict[str, object]:
    response = await client.get(profile_url(user_id), headers=bearer(api_token(auth_id)))
    assert response.status_code == 200, response.text
    return dict(response.json())


# --------------------------------------------------------------------------
# The read moves to the primary offering
# --------------------------------------------------------------------------


async def test_the_owner_profile_reads_its_settings_from_the_primary_offering(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The positive case.

    Written when `mentor_profiles` still carried both columns, so it set the two
    locations to *opposite* values and only the new source could produce this
    answer. The contract step removed the old location entirely, which is a
    stronger guarantee than the fixture was: there is no second place a reader
    could be looking. Non-default values are kept anyway, so a reader returning
    column defaults still fails.
    """
    mentor, auth_id = await as_mentor(db_engine, "reads-primary")
    session_type = await add_session_type(db_engine, mentor, name="Mock interview", venue="zoom")
    await make_primary(db_engine, mentor, session_type)
    await set_confirmation(db_engine, session_type, True)

    profile = await read_profile(api_client, mentor, auth_id)

    assert profile["default_meeting_venue"] == "zoom"
    assert profile["requires_booking_confirmation"] is True


async def test_a_mentor_with_no_primary_reports_no_booking_settings(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Null, not a default, and **not a 500**.

    Both fields were required on the response model, which is what made the
    terminus question blocking: a mentor with no primary is ordinary, and a
    required field with no value is a serialisation error on the owner's own
    profile.
    """
    mentor, auth_id = await as_mentor(db_engine, "no-primary")

    profile = await read_profile(api_client, mentor, auth_id)

    assert profile["default_meeting_venue"] is None
    assert profile["requires_booking_confirmation"] is None


async def test_a_released_pointer_still_reads_rather_than_failing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The state the guard creates: live offerings, no primary.

    This is the exact configuration a mentor is in between the two steps of
    swapping which offering is primary, so it must be a legible profile rather
    than an error — and the public offerings must still resolve a venue, which
    they do because they carry their own.
    """
    mentor, auth_id = await as_mentor(db_engine, "released")
    session_type = await add_session_type(db_engine, mentor, name="SOP review", venue="daily")
    await make_primary(db_engine, mentor, session_type)
    await release_primary(db_engine, mentor)

    profile = await read_profile(api_client, mentor, auth_id)
    offerings = (await api_client.get(f"/api/v1/users/{mentor}/session-types")).json()["data"]

    assert profile["default_meeting_venue"] is None
    assert [o["meeting_venue"] for o in offerings] == ["daily"], (
        "an offering stopped resolving a venue once its mentor had no primary"
    )


# --------------------------------------------------------------------------
# The write reaches the new location
# --------------------------------------------------------------------------


async def test_toggling_confirmation_survives_a_read(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Write-then-read through the API, which is the only shape that sees this.

    Asserting the 200, or asserting `mentor_profiles`, both pass against a
    writer that never reaches the offering.
    """
    mentor, auth_id = await as_mentor(db_engine, "toggle")
    session_type = await add_session_type(db_engine, mentor, name="SOP review")
    await make_primary(db_engine, mentor, session_type)

    response = await api_client.patch(
        profile_url(mentor),
        json={"requires_booking_confirmation": True},
        headers=bearer(api_token(auth_id)),
    )
    assert response.status_code in (200, 204), response.text

    profile = await read_profile(api_client, mentor, auth_id)
    assert profile["requires_booking_confirmation"] is True


async def test_the_toggle_reaches_every_live_offering(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """All of them, not only the primary.

    The column has no inherit, and this endpoint is the mentor's only control
    over it — so touching the primary alone would leave their other offerings on
    whatever the backfill wrote, with no way to reach them.
    """
    mentor, auth_id = await as_mentor(db_engine, "fan-out")
    primary = await add_session_type(db_engine, mentor, name="SOP review")
    await add_session_type(db_engine, mentor, name="Mock interview")
    await make_primary(db_engine, mentor, primary)

    await api_client.patch(
        profile_url(mentor),
        json={"requires_booking_confirmation": True},
        headers=bearer(api_token(auth_id)),
    )

    assert await confirmations(db_engine, mentor) == {"SOP review": True, "Mock interview": True}


async def test_the_toggle_does_not_reach_another_mentors_offerings(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The scoping half. A fan-out missing its `WHERE` changes the platform."""
    mentor, auth_id = await as_mentor(db_engine, "mine")
    mine = await add_session_type(db_engine, mentor, name="Mine")
    await make_primary(db_engine, mentor, mine)
    other = await make_public_mentor(db_engine, "theirs")
    await add_session_type(db_engine, other, name="Theirs")

    await api_client.patch(
        profile_url(mentor),
        json={"requires_booking_confirmation": True},
        headers=bearer(api_token(auth_id)),
    )

    assert await confirmations(db_engine, other) == {"Theirs": False}


async def test_an_explicit_null_confirmation_is_refused_rather_than_a_500(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`bool`, not `bool | None`, and this is the difference.

    Every route dumps with `exclude_unset=True`, so `| None` was never what made
    the field optional — omitting it already worked. What it bought was the
    right to send an explicit `null`, which `_sent` forwarded to a `NOT NULL`
    column: an authenticated 500 on a value the schema advertised as legal.

    Pre-existing rather than introduced here, and fixed here because this is the
    field the release is about and the fan-out would otherwise need a dead
    `None` branch to guard a case the boundary should never admit.
    """
    mentor, auth_id = await as_mentor(db_engine, "null-confirm")
    session_type = await add_session_type(db_engine, mentor, name="SOP review")
    await make_primary(db_engine, mentor, session_type)
    await set_confirmation(db_engine, session_type, True)

    response = await api_client.patch(
        profile_url(mentor),
        json={"requires_booking_confirmation": None},
        headers=bearer(api_token(auth_id)),
    )

    assert response.status_code == 422, response.text
    assert await confirmations(db_engine, mentor) == {"SOP review": True}


async def test_a_patch_that_says_nothing_about_confirmation_leaves_it_alone(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Absent is not false.

    `_sent` already draws this line for the mentor row; the fan-out has to draw
    it again, because a `PATCH` of the headline alone would otherwise reset
    every offering's booking policy.
    """
    mentor, auth_id = await as_mentor(db_engine, "untouched")
    session_type = await add_session_type(db_engine, mentor, name="SOP review")
    await make_primary(db_engine, mentor, session_type)
    await set_confirmation(db_engine, session_type, True)

    await api_client.patch(
        profile_url(mentor),
        json={"headline": "I help with applications"},
        headers=bearer(api_token(auth_id)),
    )

    assert await confirmations(db_engine, mentor) == {"SOP review": True}
