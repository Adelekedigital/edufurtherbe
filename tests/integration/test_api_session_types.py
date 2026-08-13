"""What a mentor offers, publicly — and the endpoint that makes `/slots` usable.

The second read here with no viewer. As with slots, the refusal tests are about
the *mentor's* state rather than about tokens, because there is no token.

The test that matters most is at the bottom: an id taken from this endpoint,
passed straight to `/slots`. That chain is the only reason this endpoint exists,
and nothing else in either suite asserts it.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import add_session_type, make_public_mentor

pytestmark = [pytest.mark.db, pytest.mark.anyio]


def url(mentor: object) -> str:
    return f"/api/v1/users/{mentor}/session-types"


# --------------------------------------------------------------------------
# Public, and what public is bounded by
# --------------------------------------------------------------------------


async def test_a_mentors_offerings_are_readable_without_a_token(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "public")
    await add_session_type(db_engine, mentor, name="SOP review", duration=45, notice=120)

    response = await api_client.get(url(mentor))

    assert response.status_code == 200
    assert response.json()["next_cursor"] is None
    (offering,) = response.json()["data"]
    assert offering["name"] == "SOP review"
    assert offering["duration_minutes"] == 45
    assert offering["min_notice_minutes"] == 120


@pytest.mark.parametrize(
    ("tag", "knob"),
    [("unlisted", {"listed": False}), ("unapproved", {"approved": False})],
)
async def test_a_mentor_who_is_not_public_is_a_404(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, tag: str, knob: dict[str, bool]
) -> None:
    """Both halves of the predicate, separately.

    `apply_mentor_status` writes approval or listing and never both, and no
    CHECK ties them — so `pending` + `listed` is a legal row and listing alone
    would publish an unvetted mentor.
    """
    mentor = await make_public_mentor(db_engine, tag, **knob)
    await add_session_type(db_engine, mentor)

    assert (await api_client.get(url(mentor))).status_code == 404


async def test_a_user_who_is_not_a_mentor_is_a_404(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """No mentor profile at all — indistinguishable from an unlisted one."""
    async with db_engine.begin() as conn:
        from uuid import uuid4

        user = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
                    "VALUES ('mentee-only@example.test', :a, 'Bo', 'mentee', 'UTC') RETURNING id"
                ),
                {"a": uuid4()},
            )
        ).scalar_one()

    assert (await api_client.get(url(user))).status_code == 404


async def test_a_public_mentor_offering_nothing_is_an_empty_page_not_a_404(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Two different statements, and collapsing them would say a mentor who has
    switched everything off does not exist."""
    mentor = await make_public_mentor(db_engine, "nothing")

    response = await api_client.get(url(mentor))

    assert response.status_code == 200
    assert response.json()["data"] == []


@pytest.mark.parametrize(
    ("tag", "knob"),
    [("inactive", {"active": False}), ("deleted", {"deleted": True})],
)
async def test_an_offering_that_is_off_or_deleted_is_absent(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, tag: str, knob: dict[str, bool]
) -> None:
    """`is_active` covers off, closed and hidden alike (#90); `deleted_at` is
    separate. One covers the other in every happy path, so each gets a test."""
    mentor = await make_public_mentor(db_engine, f"gone-{tag}")
    await add_session_type(db_engine, mentor, name="Live one")
    await add_session_type(db_engine, mentor, name="Gone", **knob)

    response = await api_client.get(url(mentor))

    assert [o["name"] for o in response.json()["data"]] == ["Live one"]


# --------------------------------------------------------------------------
# What the response says, and what it must never say
# --------------------------------------------------------------------------


async def test_a_venue_of_its_own_is_returned_as_is(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "own-venue", default_venue="google_meet")
    await add_session_type(db_engine, mentor, venue="zoom")

    (offering,) = (await api_client.get(url(mentor))).json()["data"]

    assert offering["meeting_venue"] == "zoom"


async def test_a_null_venue_inherits_the_mentors_default(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Null on the config means *inherit* (D21), not "unset".

    Returning the raw null would make every client implement the cascade, and a
    client that gets it wrong shows "no venue" for a mentor who has one.
    """
    mentor = await make_public_mentor(db_engine, "inherit", default_venue="google_meet")
    await add_session_type(db_engine, mentor, venue=None)

    (offering,) = (await api_client.get(url(mentor))).json()["data"]

    assert offering["meeting_venue"] == "google_meet"


async def test_the_resolved_venue_is_never_null(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """There is no "neither set" state, which is why the field is required.

    `session_type_booking_configs.meeting_venue` is nullable, so the first draft
    of the response model made this optional by looking only at that column. The
    fallback is `mentor_profiles.default_meeting_venue`, which is **NOT NULL with
    a server default of `google_meet`** — so the COALESCE cannot produce null and
    an optional field would have promised a case that cannot arise.
    """
    mentor = await make_public_mentor(db_engine, "always-venue")
    await add_session_type(db_engine, mentor, venue=None)

    (offering,) = (await api_client.get(url(mentor))).json()["data"]

    assert offering["meeting_venue"] == "google_meet"


async def test_the_response_carries_nothing_it_should_not(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """An allowlist, asserted as one.

    `custom_meeting_url` is a **static room link** — a bearer credential anyone
    holding it can walk into, which is why per-session links exist at all. It
    must never reach a public response. `category` and `application_stage` are
    free text with no vocabulary, and publishing them would commit this contract
    to a shape nobody has designed.
    """
    mentor = await make_public_mentor(
        db_engine,
        "leak",
        # A custom URL requires the custom venue: there is a CHECK, and it
        # runs one way only — the venue does not require a URL.
        default_venue="custom",
        custom_meeting_url="https://meet.google.com/abc-defg-hij",
    )
    await add_session_type(
        db_engine, mentor, category="internal-classification", application_stage="postgrad"
    )

    response = await api_client.get(url(mentor))
    body = response.text
    (offering,) = response.json()["data"]

    assert set(offering) == {
        "id",
        "name",
        "description",
        "duration_minutes",
        "min_notice_minutes",
        "meeting_venue",
    }
    assert "abc-defg-hij" not in body, "a static meeting room reached a public response"
    assert "internal-classification" not in body
    assert "postgrad" not in body


async def test_offerings_are_ordered_by_name(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Name is unique per mentor among live rows, so the order is total."""
    mentor = await make_public_mentor(db_engine, "ordered")
    for name in ("Visa questions", "Application review", "Mock interview"):
        await add_session_type(db_engine, mentor, name=name)

    names = [o["name"] for o in (await api_client.get(url(mentor))).json()["data"]]

    assert names == ["Application review", "Mock interview", "Visa questions"]


# --------------------------------------------------------------------------
# The reason this endpoint exists
# --------------------------------------------------------------------------


async def test_an_id_from_here_works_against_the_slots_endpoint(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The chain, end to end, with no id known in advance.

    `/slots` requires a `session_type_id` and nothing handed one out until this
    shipped. A client discovers the offering here and asks for its slots — which
    is the whole point, and is asserted nowhere else in either suite.
    """
    mentor = await make_public_mentor(db_engine, "chain")
    await add_session_type(db_engine, mentor, duration=60, notice=0)
    async with db_engine.begin() as conn:
        for day_of_week in range(7):
            await conn.execute(
                text(
                    "INSERT INTO availability_rules "
                    "(mentor_user_id, day_of_week, start_time, end_time, timezone) "
                    "VALUES (:u, :d, '09:00', '12:00', 'Africa/Lagos')"
                ),
                {"u": mentor, "d": day_of_week},
            )

    discovered = (await api_client.get(url(mentor))).json()["data"]
    assert len(discovered) == 1

    slots = await api_client.get(
        f"/api/v1/users/{mentor}/availability/slots?session_type_id={discovered[0]['id']}"
    )

    assert slots.status_code == 200
    assert slots.json()["data"], "the discovered offering produced no slots"


async def test_the_list_holds_only_this_mentors_offerings(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The owner scope, tested against a database that contains someone else.

    Without `mentor_user_id` in the WHERE this returns every mentor's offerings
    to every caller. A single-mentor fixture cannot tell that apart from correct
    behaviour, which is exactly how a scoping bug survives a green suite.
    """
    mine = await make_public_mentor(db_engine, "scope-mine")
    theirs = await make_public_mentor(db_engine, "scope-theirs")
    await add_session_type(db_engine, mine, name="Mine")
    await add_session_type(db_engine, theirs, name="Theirs")

    names = [o["name"] for o in (await api_client.get(url(mine))).json()["data"]]

    assert names == ["Mine"]


async def test_a_soft_deleted_mentor_profile_is_not_public(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Approved and listed is not sufficient — the profile must also still exist.

    `mentor_profiles` carries `deleted_at`, and the status columns are untouched
    by a soft delete: a removed mentor stays `approved` + `listed` on the row.
    Checking only those two publishes a mentor who has been deleted.
    """
    mentor = await make_public_mentor(db_engine, "soft-deleted")
    await add_session_type(db_engine, mentor)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE mentor_profiles SET deleted_at = now() WHERE user_id = :u"),
            {"u": mentor},
        )

    assert (await api_client.get(url(mentor))).status_code == 404


async def test_an_offering_with_no_booking_config_is_absent(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """An inner join, and the behaviour it produces is the intended one.

    Without a config there is no duration, so there is no slot length and
    nothing bookable — and `/slots` already 404s for such an offering. Listing
    it here would advertise something no caller could act on.

    Pinned because it is join *shape* rather than an explicit predicate: a
    later change to `LEFT JOIN`, made to support the inheritance in #88, would
    start publishing unconfigured offerings with null durations and nothing
    else would notice.
    """
    mentor = await make_public_mentor(db_engine, "no-config")
    await add_session_type(db_engine, mentor, name="Configured")
    await add_session_type(db_engine, mentor, name="Unconfigured", config=False)

    names = [o["name"] for o in (await api_client.get(url(mentor))).json()["data"]]

    assert names == ["Configured"]


async def test_a_soft_deleted_user_is_not_public(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The **second** soft delete, on the table beside the one already checked.

    A mentor is a `users` row and a `mentor_profiles` row, each with its own
    `deleted_at`, and nothing ties them together: deleting the user leaves the
    profile approved, listed and undeleted. The predicate checked the profile's
    and published the offerings of a user who no longer exists.
    """
    mentor = await make_public_mentor(db_engine, "deleted-user")
    await add_session_type(db_engine, mentor)
    async with db_engine.begin() as conn:
        await conn.execute(text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": mentor})

    assert (await api_client.get(url(mentor))).status_code == 404
