"""A mentor holds one offering on its own video provider (decision 288).

`meeting_venue` on `POST`/`PATCH /me/session-types` is `daily`, `google_meet` or
`null`. A value points the offering at the mentor's own option row for that
provider, creating it as a **non-default** row if missing; `null` follows the
mentor's default. `custom` is refused: it needs a URL, and per-offering URLs are
out of scope.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from tests.integration.factories import add_session_type, make_public_mentor
from tests.integration.test_api_me_session_type_writes import as_mentor, body
from tests.integration.test_conferencing_options import add_option

from app.domain.enums import ConferencingProvider
from app.infra.db.conferencing_store import offering_option, set_default_option
from app.infra.db.session_type_store import resolve_venue
from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

URL = "/api/v1/me/session-types"
CONFERENCING = "/api/v1/me/conferencing"
ROOM = "https://rooms.example.org/ada"


async def options_of(engine: AsyncEngine, mentor: UUID) -> set[tuple[str, bool]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT provider, is_default FROM mentor_conferencing_options WHERE user_id = :u"),
            {"u": mentor},
        )
        return {(str(row[0]), bool(row[1])) for row in rows}


async def pin_of(engine: AsyncEngine, session_type: str | UUID) -> str | None:
    """The provider the offering is pinned to, or ``None`` when it inherits."""
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT o.provider FROM session_types st "
                    "LEFT JOIN mentor_conferencing_options o "
                    "  ON o.id = st.conferencing_option_id AND o.user_id = st.mentor_user_id "
                    "WHERE st.id = :t"
                ),
                {"t": session_type},
            )
        ).scalar_one()


async def own(client: httpx.AsyncClient, auth_id: UUID) -> dict[str, object]:
    (offering,) = (await client.get(URL, headers=bearer(api_token(auth_id)))).json()["data"]
    return dict(offering)


# --------------------------------------------------------------------------
# Create
# --------------------------------------------------------------------------


async def test_creating_pinned_makes_a_non_default_option_and_keeps_the_default(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A mentor with no option at all: the pin creates one, never as the default,
    so `/me/conferencing` still reports the platform's choice."""
    mentor, auth_id = await as_mentor(db_engine, "venue-create")
    headers = bearer(api_token(auth_id))

    created = await api_client.post(URL, json=body(meeting_venue="google_meet"), headers=headers)

    assert created.status_code == 201, created.text
    assert await options_of(db_engine, mentor) == {("google_meet", False)}
    assert await pin_of(db_engine, created.json()["id"]) == "google_meet"
    offering = await own(api_client, auth_id)
    assert (offering["meeting_venue"], offering["meeting_venue_inherited"]) == (
        "google_meet",
        False,
    )
    conferencing = (await api_client.get(CONFERENCING, headers=headers)).json()
    assert (conferencing["provider"], conferencing["is_default_choice"]) == ("daily", True)


async def test_pinning_reuses_the_mentors_row_for_that_provider(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Upserted on `(user_id, provider)`: pinning onto the mentor's default
    provider points at the default row itself, and the default stays it."""
    mentor, auth_id = await as_mentor(db_engine, "venue-reuse")
    headers = bearer(api_token(auth_id))
    default = await add_option(db_engine, mentor, provider="daily", is_default=True)

    created = await api_client.post(URL, json=body(meeting_venue="daily"), headers=headers)

    assert created.status_code == 201, created.text
    assert await options_of(db_engine, mentor) == {("daily", True)}
    async with db_engine.connect() as conn:
        pointer = (
            await conn.execute(
                text("SELECT conferencing_option_id FROM session_types WHERE id = :t"),
                {"t": created.json()["id"]},
            )
        ).scalar_one()
    assert pointer == default
    assert (await own(api_client, auth_id))["meeting_venue_inherited"] is False


async def test_pinning_onto_a_non_default_row_leaves_it_non_default(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The conflict branch of the upsert: an existing Meet row that is not the
    default is reused as it is, and the Daily default keeps its place."""
    mentor, auth_id = await as_mentor(db_engine, "venue-non-default")
    await add_option(db_engine, mentor, provider="daily", is_default=True)
    meet = await add_option(db_engine, mentor, provider="google_meet")

    created = await api_client.post(
        URL, json=body(meeting_venue="google_meet"), headers=bearer(api_token(auth_id))
    )

    assert created.status_code == 201, created.text
    assert await options_of(db_engine, mentor) == {("daily", True), ("google_meet", False)}
    async with db_engine.connect() as conn:
        pointer = (
            await conn.execute(
                text("SELECT conferencing_option_id FROM session_types WHERE id = :t"),
                {"t": created.json()["id"]},
            )
        ).scalar_one()
    assert pointer == meet


@pytest.mark.parametrize("sent", [{}, {"meeting_venue": None}], ids=["omitted", "null"])
async def test_creating_without_a_venue_follows_the_default(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, sent: dict[str, object]
) -> None:
    mentor, auth_id = await as_mentor(db_engine, "venue-inherit")
    await add_option(db_engine, mentor, provider="google_meet", is_default=True)

    created = await api_client.post(URL, json=body(**sent), headers=bearer(api_token(auth_id)))

    assert created.status_code == 201, created.text
    assert await pin_of(db_engine, created.json()["id"]) is None
    assert await options_of(db_engine, mentor) == {("google_meet", True)}
    offering = await own(api_client, auth_id)
    assert (offering["meeting_venue"], offering["meeting_venue_inherited"]) == (
        "google_meet",
        True,
    )


# --------------------------------------------------------------------------
# Change
# --------------------------------------------------------------------------


async def test_patching_pins_leaves_and_resets(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A value pins, an omitted field leaves the pin, an explicit `null` resets
    the offering to follow the default."""
    mentor, auth_id = await as_mentor(db_engine, "venue-patch")
    headers = bearer(api_token(auth_id))
    await add_option(db_engine, mentor, provider="daily", is_default=True)
    session_type = await add_session_type(db_engine, mentor, name="SOP review")
    item = f"{URL}/{session_type}"

    pinned = await api_client.patch(item, json={"meeting_venue": "google_meet"}, headers=headers)
    assert pinned.status_code == 200, pinned.text
    assert await pin_of(db_engine, session_type) == "google_meet"

    renamed = await api_client.patch(item, json={"name": "Renamed"}, headers=headers)
    assert renamed.status_code == 200, renamed.text
    assert await pin_of(db_engine, session_type) == "google_meet"

    reset = await api_client.patch(item, json={"meeting_venue": None}, headers=headers)
    assert reset.status_code == 200, reset.text
    assert await pin_of(db_engine, session_type) is None
    offering = await own(api_client, auth_id)
    assert (offering["meeting_venue"], offering["meeting_venue_inherited"]) == ("daily", True)
    # The row the pin made stays, non-default, for the next pin to reuse.
    assert await options_of(db_engine, mentor) == {("daily", True), ("google_meet", False)}


@pytest.mark.parametrize("venue", ["custom", "zoom", "teams"])
async def test_a_venue_an_offering_cannot_hold_is_refused_at_its_field(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, venue: str
) -> None:
    """`custom` needs a URL, which only `/me/conferencing` takes; the others are
    not providers at all. Both writes refuse, and nothing is written."""
    mentor, auth_id = await as_mentor(db_engine, f"venue-refused-{venue}")
    headers = bearer(api_token(auth_id))
    session_type = await add_session_type(db_engine, mentor, name="Held")

    created = await api_client.post(
        URL, json=body(name="New", meeting_venue=venue), headers=headers
    )
    patched = await api_client.patch(
        f"{URL}/{session_type}", json={"meeting_venue": venue}, headers=headers
    )

    for refused in (created, patched):
        assert refused.status_code == 422, refused.text
        assert [e["pointer"] for e in refused.json()["errors"]] == ["/meeting_venue"]
        # One message for every refusal, never Pydantic's list offering `custom`.
        assert "daily or google_meet" in refused.json()["errors"][0]["message"]
    assert await options_of(db_engine, mentor) == set()
    assert await pin_of(db_engine, session_type) is None


async def test_another_mentors_offering_is_not_found_and_creates_no_option(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Scoped before the option is written, so a refused `PATCH` leaves no row
    behind for the caller and no pin on theirs."""
    mine, auth_id = await as_mentor(db_engine, "venue-mine")
    other = await make_public_mentor(db_engine, "venue-theirs")
    theirs = await add_session_type(db_engine, other, name="Theirs")

    refused = await api_client.patch(
        f"{URL}/{theirs}", json={"meeting_venue": "google_meet"}, headers=bearer(api_token(auth_id))
    )

    assert refused.status_code == 404, refused.text
    assert await options_of(db_engine, mine) == set()
    assert await options_of(db_engine, other) == set()
    assert await pin_of(db_engine, theirs) is None


# --------------------------------------------------------------------------
# The mentor's default moving underneath a pin
# --------------------------------------------------------------------------


async def test_switching_the_default_never_moves_a_pinned_offering(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`/me/conferencing` upserts on `(user_id, provider)` and only flips
    `is_default`, so the pinned row keeps its id: making it the default, then
    moving the default away to a personal link, leaves the offering on Meet."""
    mentor, auth_id = await as_mentor(db_engine, "venue-switch")
    headers = bearer(api_token(auth_id))
    pinned = (
        await api_client.post(URL, json=body(meeting_venue="google_meet"), headers=headers)
    ).json()["id"]
    unpinned = (await api_client.post(URL, json=body(name="Follows"), headers=headers)).json()["id"]

    for choice in ({"provider": "google_meet"}, {"provider": "custom", "custom_url": ROOM}):
        switched = await api_client.patch(CONFERENCING, json=choice, headers=headers)
        assert switched.status_code == 200, switched.text
        assert await pin_of(db_engine, pinned) == "google_meet"

    listed = (await api_client.get(URL, headers=headers)).json()["data"]
    venues = {o["id"]: (o["meeting_venue"], o["meeting_venue_inherited"]) for o in listed}
    assert venues == {pinned: ("google_meet", False), unpinned: ("custom", True)}
    assert await options_of(db_engine, mentor) == {("google_meet", False), ("custom", True)}


async def test_the_public_list_shows_the_pinned_venue_and_not_the_flag(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, auth_id = await as_mentor(db_engine, "venue-public")
    await api_client.post(
        URL, json=body(meeting_venue="google_meet"), headers=bearer(api_token(auth_id))
    )

    (offering,) = (await api_client.get(f"/api/v1/users/{mentor}/session-types")).json()["data"]

    assert offering["meeting_venue"] == "google_meet"
    assert "meeting_venue_inherited" not in offering


async def test_a_pin_takes_no_link_from_the_default(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Provider and link come from the same row (#127): an offering pinned to
    Meet under a personal-link default resolves to Meet with no link."""
    _, auth_id = await as_mentor(db_engine, "venue-no-link")
    headers = bearer(api_token(auth_id))
    await api_client.patch(
        CONFERENCING, json={"provider": "custom", "custom_url": ROOM}, headers=headers
    )
    created = await api_client.post(URL, json=body(meeting_venue="google_meet"), headers=headers)

    async with AsyncSession(db_engine) as session:
        resolved = await resolve_venue(session, UUID(created.json()["id"]))

    assert resolved == ("google_meet", None)


async def _waiting(engine: AsyncEngine) -> bool:
    """Whether some backend is blocked on a lock right now."""
    async with engine.connect() as conn:
        found = await conn.execute(
            text("SELECT 1 FROM pg_stat_activity WHERE wait_event_type = 'Lock' LIMIT 1")
        )
        return found.first() is not None


async def test_a_pin_and_a_default_switch_wait_for_each_other_and_never_deadlock(
    db_engine: AsyncEngine,
) -> None:
    """The switch holds `FOR UPDATE` on the profile and upserts Meet; a pin of
    Meet started meanwhile must wait **before** inserting. Inserting first, its
    foreign key would wait on the switch while the switch's upsert waited on the
    new row: a deadlock, a 500."""
    mentor = await make_public_mentor(db_engine, "venue-deadlock")
    switch, pin = AsyncSession(db_engine), AsyncSession(db_engine)
    try:
        await switch.execute(
            text("SELECT 1 FROM mentor_profiles WHERE user_id = :u FOR UPDATE"), {"u": mentor}
        )
        pinning = asyncio.create_task(
            offering_option(pin, mentor, ConferencingProvider.GOOGLE_MEET)
        )
        for _ in range(100):
            if await _waiting(db_engine):
                break
            await asyncio.sleep(0.05)
        await set_default_option(switch, mentor, ConferencingProvider.GOOGLE_MEET, None)
        await switch.commit()
        option = await asyncio.wait_for(pinning, timeout=10)
        await pin.commit()
    finally:
        await switch.close()
        await pin.close()

    assert option is not None
    assert await options_of(db_engine, mentor) == {("google_meet", True)}
