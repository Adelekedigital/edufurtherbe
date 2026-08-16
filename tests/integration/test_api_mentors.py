"""The public mentor profile — the third read with no viewer, and the first
reachable by two different handles.

**Every refusal is tested by id *and* by slug.** A second lookup path is where a
visibility clause goes missing, and that is not a hypothetical here: every bug
found in this milestone's public endpoints has been a predicate present on one
path and absent from another. The parametrisation is the point.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import add_session_type, make_public_mentor

pytestmark = [pytest.mark.db, pytest.mark.anyio]


def url(handle: object) -> str:
    return f"/api/v1/mentors/{handle}"


async def give_profile(
    engine: AsyncEngine,
    user_id: UUID,
    *,
    about: str = "I help with SOPs.",
    avatar: str | None = "https://cdn.test/a.png",
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_profiles (user_id, about_me, avatar_url, social_linkedin) "
                "VALUES (:u, :b, :a, 'https://linkedin.test/ada')"
            ),
            {"u": user_id, "b": about, "a": avatar},
        )


async def give_offering(engine: AsyncEngine, user_id: UUID, slug: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO mentor_service_offerings (mentor_user_id, service_offering_id) "
                "SELECT :u, id FROM service_offerings WHERE slug = :s"
            ),
            {"u": user_id, "s": slug},
        )


async def set_study_country(engine: AsyncEngine, user_id: UUID, code: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE mentor_profiles SET primary_study_country_id = "
                "(SELECT id FROM countries WHERE code = :c) WHERE user_id = :u"
            ),
            {"u": user_id, "c": code},
        )


# --------------------------------------------------------------------------
# Reachable, by either handle
# --------------------------------------------------------------------------


async def test_a_mentor_is_readable_by_id_without_a_token(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "by-id", slug="ada-by-id")
    await give_profile(db_engine, mentor)

    response = await api_client.get(url(mentor))

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(mentor)
    assert body["first_name"] == "Ada"
    assert body["headline"] == "M"
    assert body["about_me"] == "I help with SOPs."


async def test_the_same_mentor_is_readable_by_slug(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The legacy profile link keeps working — decision #28's whole reason."""
    mentor = await make_public_mentor(db_engine, "by-slug", slug="ada-by-slug")
    await give_profile(db_engine, mentor)

    body = (await api_client.get(url("ada-by-slug"))).json()

    assert body["id"] == str(mentor)
    assert body["slug"] == "ada-by-slug"


async def test_a_mentor_with_no_slug_is_still_reachable_by_id(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`users.slug` is nullable and null on 4 of 43 migrated users."""
    mentor = await make_public_mentor(db_engine, "no-slug")

    body = (await api_client.get(url(mentor))).json()

    assert body["slug"] is None
    assert body["id"] == str(mentor)


async def test_a_mentor_with_no_user_profile_row_still_renders(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The join is outer. A mentor who never wrote a bio has no `user_profiles`
    row at all, and an inner join would 404 them — indistinguishable from "not
    listed", and debugged in the authorization code where the defect is not."""
    mentor = await make_public_mentor(db_engine, "no-user-profile", slug="bare")

    response = await api_client.get(url(mentor))

    assert response.status_code == 200
    assert response.json()["about_me"] is None
    assert response.json()["avatar_url"] is None


# --------------------------------------------------------------------------
# Refusals — every reason, on both handles
# --------------------------------------------------------------------------


@pytest.mark.parametrize("by", ["id", "slug"])
@pytest.mark.parametrize(
    ("reason", "knob"),
    [("unlisted", {"listed": False}), ("unapproved", {"approved": False})],
)
async def test_a_mentor_who_is_not_public_is_a_404(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, by: str, reason: str, knob: dict
) -> None:
    slug = f"{reason}-{by}"
    mentor = await make_public_mentor(db_engine, slug, slug=slug, **knob)

    response = await api_client.get(url(mentor if by == "id" else slug))

    assert response.status_code == 404


@pytest.mark.parametrize("by", ["id", "slug"])
@pytest.mark.parametrize("table", ["mentor_profiles", "users"])
async def test_a_soft_deleted_mentor_is_a_404(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, by: str, table: str
) -> None:
    """**Two soft deletes on two tables**, each with its own `deleted_at` and
    nothing tying them. Both shipped as bugs on the other public endpoints, one
    sweep apart, so both are asserted here on both handles."""
    # `ck_users_slug_is_url_safe` is `^[a-z0-9-]+$` — no underscores, which
    # the table name carries. The same CHECK is why a UUID string is a legal
    # slug, and why the handle is parsed rather than pattern-matched.
    slug = f"deleted-{table.replace('_', '-')}-{by}"
    mentor = await make_public_mentor(db_engine, slug, slug=slug)
    statement = {
        "mentor_profiles": "UPDATE mentor_profiles SET deleted_at = now() WHERE user_id = :u",
        "users": "UPDATE users SET deleted_at = now() WHERE id = :u",
    }[table]
    async with db_engine.begin() as conn:
        await conn.execute(text(statement), {"u": mentor})

    response = await api_client.get(url(mentor if by == "id" else slug))

    assert response.status_code == 404


async def test_a_user_who_is_not_a_mentor_is_a_404(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    async with db_engine.begin() as conn:
        mentee = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
                    "VALUES ('plain@example.test', :a, 'Bo', 'mentee', 'UTC') RETURNING id"
                ),
                {"a": uuid4()},
            )
        ).scalar_one()

    assert (await api_client.get(url(mentee))).status_code == 404


@pytest.mark.parametrize("handle", ["nobody-at-all", "019ff5c8-bb86-7f52-b01f-026ad188b66e"])
async def test_an_unknown_handle_is_a_404(api_client: httpx.AsyncClient, handle: str) -> None:
    """A slug that matches nobody and a well-formed id that matches nobody get
    the same answer — the id is not rejected differently for being parseable."""
    assert (await api_client.get(url(handle))).status_code == 404


# --------------------------------------------------------------------------
# What the response carries
# --------------------------------------------------------------------------


async def test_the_inlined_session_types_match_the_standalone_endpoint(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """One function, two doors. Asserted against each other rather than against
    a hand-written expectation, so the two cannot drift apart later."""
    mentor = await make_public_mentor(db_engine, "inline", slug="inline")
    await add_session_type(db_engine, mentor, name="Deep dive", duration=60)
    await add_session_type(db_engine, mentor, name="Quick chat", duration=15)

    inlined = (await api_client.get(url(mentor))).json()["session_types"]
    standalone = (await api_client.get(f"/api/v1/users/{mentor}/session-types")).json()["data"]

    assert inlined == standalone
    assert [t["name"] for t in inlined] == ["Deep dive", "Quick chat"]


async def test_offerings_are_the_taxonomy_in_platform_order(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`sort_order`, not alphabetical: the platform decides how these read."""
    mentor = await make_public_mentor(db_engine, "offerings", slug="offerings")
    await give_offering(db_engine, mentor, "interview-preparation")  # sort_order 60
    await give_offering(db_engine, mentor, "test-preparation")  # sort_order 10

    body = (await api_client.get(url(mentor))).json()

    assert [o["slug"] for o in body["offerings"]] == [
        "test-preparation",
        "interview-preparation",
    ]


async def test_offerings_are_scoped_to_the_mentor_asked_for(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A second mentor, so `mentor_user_id` can fail.

    With one mentor in the database the scope predicate is unfalsifiable — every
    row belongs to them, so dropping it returns the same list. This is the shape
    non-negotiable #5 names: object-level authorization scoped in the query, and
    a scope nothing can fail is not pinned.
    """
    asked_for = await make_public_mentor(db_engine, "scoped-a", slug="scoped-a")
    somebody_else = await make_public_mentor(db_engine, "scoped-b", slug="scoped-b")
    await give_offering(db_engine, asked_for, "test-preparation")
    await give_offering(db_engine, somebody_else, "interview-preparation")

    body = (await api_client.get(url(asked_for))).json()

    assert [o["slug"] for o in body["offerings"]] == ["test-preparation"]


async def test_countries_are_names_not_identifiers(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Returning a `countries.id` would reproduce exactly the gap the party
    identity change closed: correct, and unusable without a call this API does
    not offer."""
    mentor = await make_public_mentor(db_engine, "country", slug="country")
    await set_study_country(db_engine, mentor, "GB")

    body = (await api_client.get(url(mentor))).json()

    assert body["primary_study_country"] == "United Kingdom"


async def test_no_private_field_reaches_the_public_profile(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The allowlist, asserted rather than assumed.

    **`custom_meeting_url` was the sharpest case and is gone.** A static room
    link is a bearer credential anyone holding it can walk into, and this test
    proved one never reached the public profile — until D88's contract step
    removed the column, at which point the assertion could no longer fail. The
    email and status assertions below are the live half.

    If booking reintroduces a URL for the custom venue, the assertion comes back
    with it. That is the condition, written down rather than left to whoever
    notices.
    """
    mentor = await make_public_mentor(db_engine, "private", slug="private")
    await give_profile(db_engine, mentor)

    raw = (await api_client.get(url(mentor))).text

    assert "mentor-private@example.test" not in raw
    assert "email" not in raw
    assert "approval_status" not in raw
    assert "listing_status" not in raw
    assert "gender" not in raw
