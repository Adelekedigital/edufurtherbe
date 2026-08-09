"""One user's education, goals, awards and mentor profile — and who may read them.

**This file exists for the refusals.** Returning a user their own records proves
almost nothing; returning them somebody else's is the defect that matters, and it
is silent — a leaking endpoint answers 200 and looks exactly like a working one.

So every endpoint gets its own explicit refusal test. Not a parametrised sweep:
a decorator that silently covers three of four endpoints reads as complete, and
the fourth is the one that ships. This project has already paid for that shape
once, with `deleted_at IS NULL` typed into five statements and missed on the
fifth.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

#: Every scoped endpoint, so a new one added without a refusal test is visible.
SCOPED = ("education", "goals", "awards", "mentor-profile")


async def make_user(
    engine: AsyncEngine, auth_id: UUID, email: str, *, admin: bool = False, revoked: bool = False
) -> UUID:
    async with engine.begin() as conn:
        user_id = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
                    "VALUES (:e, :a, 'Ada', 'mentee', 'Africa/Lagos') RETURNING id"
                ),
                {"e": email, "a": auth_id},
            )
        ).scalar_one()
        if admin:
            # Two whole statements rather than one interpolated: `now()` is SQL
            # and cannot be a bind value, and building it by f-string is what
            # ruff's S608 exists to stop.
            await conn.execute(
                text(
                    "INSERT INTO admin_users (user_id, admin_role, revoked_at) "
                    "VALUES (:u, 'super_admin', now())"
                    if revoked
                    else "INSERT INTO admin_users (user_id, admin_role) VALUES (:u, 'super_admin')"
                ),
                {"u": user_id},
            )
    return user_id


async def add_education(engine: AsyncEngine, user_id: UUID, school: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO education_entries (user_id, school_name_raw) VALUES (:u, :s)"),
            {"u": user_id, "s": school},
        )


# --------------------------------------------------------------------------
# Refusals — one per endpoint, deliberately not parametrised away
# --------------------------------------------------------------------------


async def test_another_users_education_is_not_readable(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    owner_auth, caller_auth = uuid4(), uuid4()
    owner = await make_user(db_engine, owner_auth, "owner@example.com")
    await make_user(db_engine, caller_auth, "caller@example.com")
    await add_education(db_engine, owner, "Secret University")

    response = await api_client.get(
        f"/api/v1/users/{owner}/education", headers=bearer(api_token(caller_auth))
    )

    assert response.status_code == 404
    assert "Secret University" not in response.text


async def test_another_users_goals_are_not_readable(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    owner_auth, caller_auth = uuid4(), uuid4()
    owner = await make_user(db_engine, owner_auth, "owner@example.com")
    await make_user(db_engine, caller_auth, "caller@example.com")

    response = await api_client.get(
        f"/api/v1/users/{owner}/goals", headers=bearer(api_token(caller_auth))
    )

    assert response.status_code == 404


async def test_another_users_awards_are_not_readable(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    owner_auth, caller_auth = uuid4(), uuid4()
    owner = await make_user(db_engine, owner_auth, "owner@example.com")
    await make_user(db_engine, caller_auth, "caller@example.com")

    response = await api_client.get(
        f"/api/v1/users/{owner}/awards", headers=bearer(api_token(caller_auth))
    )

    assert response.status_code == 404


async def test_another_users_mentor_profile_is_not_readable(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    owner_auth, caller_auth = uuid4(), uuid4()
    owner = await make_user(db_engine, owner_auth, "owner@example.com")
    await make_user(db_engine, caller_auth, "caller@example.com")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO mentor_profiles (user_id, headline) VALUES (:u, 'Secret headline')"),
            {"u": owner},
        )

    response = await api_client.get(
        f"/api/v1/users/{owner}/mentor-profile", headers=bearer(api_token(caller_auth))
    )

    assert response.status_code == 404
    assert "Secret headline" not in response.text


@pytest.mark.parametrize("resource", SCOPED)
async def test_no_token_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, resource: str
) -> None:
    """Parametrised here on purpose: 401-without-a-token is the *same* assertion
    four times, unlike the refusals above where each endpoint's scope is its own
    line of SQL that could individually be wrong."""
    owner = await make_user(db_engine, uuid4(), "owner@example.com")

    assert (await api_client.get(f"/api/v1/users/{owner}/{resource}")).status_code == 401


async def test_a_nonexistent_user_is_indistinguishable_from_a_forbidden_one(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Both 404, byte for byte. A different status or body would turn this
    endpoint into an account-enumeration oracle."""
    caller_auth = uuid4()
    await make_user(db_engine, caller_auth, "caller@example.com")
    other = await make_user(db_engine, uuid4(), "other@example.com")

    absent = await api_client.get(
        f"/api/v1/users/{uuid4()}/education", headers=bearer(api_token(caller_auth))
    )
    forbidden = await api_client.get(
        f"/api/v1/users/{other}/education", headers=bearer(api_token(caller_auth))
    )

    assert absent.status_code == forbidden.status_code == 404
    assert absent.json() == forbidden.json()


async def test_a_soft_deleted_user_is_invisible_even_to_an_admin(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    admin_auth = uuid4()
    await make_user(db_engine, admin_auth, "admin@example.com", admin=True)
    target = await make_user(db_engine, uuid4(), "gone@example.com")
    async with db_engine.begin() as conn:
        await conn.execute(text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": target})

    response = await api_client.get(
        f"/api/v1/users/{target}/education", headers=bearer(api_token(admin_auth))
    )

    assert response.status_code == 404


# --------------------------------------------------------------------------
# Admin access, and its revocation
# --------------------------------------------------------------------------


async def test_an_admin_may_read_another_users_education(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The positive case the refusals above need. Without it, refusing everyone
    would pass every test in this file."""
    admin_auth = uuid4()
    await make_user(db_engine, admin_auth, "admin@example.com", admin=True)
    target = await make_user(db_engine, uuid4(), "member@example.com")
    await add_education(db_engine, target, "Reviewable University")

    response = await api_client.get(
        f"/api/v1/users/{target}/education", headers=bearer(api_token(admin_auth))
    )

    assert response.status_code == 200
    assert [e["school_name_raw"] for e in response.json()["data"]] == ["Reviewable University"]


async def test_a_revoked_admin_may_not(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`revoked_at` is what makes revocation revoke. An admin check reading only
    the row's existence would still let this caller through."""
    admin_auth = uuid4()
    await make_user(db_engine, admin_auth, "admin@example.com", admin=True, revoked=True)
    target = await make_user(db_engine, uuid4(), "member@example.com")
    await add_education(db_engine, target, "Reviewable University")

    response = await api_client.get(
        f"/api/v1/users/{target}/education", headers=bearer(api_token(admin_auth))
    )

    assert response.status_code == 404


async def test_a_user_reads_their_own_records(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "self@example.com")
    await add_education(db_engine, user_id, "My University")

    response = await api_client.get(
        f"/api/v1/users/{user_id}/education", headers=bearer(api_token(auth_id))
    )

    assert response.status_code == 200
    assert [e["school_name_raw"] for e in response.json()["data"]] == ["My University"]


async def test_a_user_sees_only_their_own_education_not_everybodys(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**Defence in depth, and a mutation proved it was missing.**

    Every refusal above stops at `TargetUserDep`, so the request never reaches
    the store and the store's own `user_id` scope is never exercised. Dropping
    that scope makes `/users/{self}/education` return *every user's* degrees to
    a caller who is perfectly entitled to their own — a 200, with other people's
    data in it, past an authorization layer that did its job.

    Two users with education is what makes that visible. One is not.
    """
    auth_id = uuid4()
    mine = await make_user(db_engine, auth_id, "self@example.com")
    theirs = await make_user(db_engine, uuid4(), "other@example.com")
    await add_education(db_engine, mine, "My University")
    await add_education(db_engine, theirs, "Their University")

    body = (
        await api_client.get(f"/api/v1/users/{mine}/education", headers=bearer(api_token(auth_id)))
    ).json()

    assert [e["school_name_raw"] for e in body["data"]] == ["My University"]


async def test_a_non_mentor_does_not_inherit_somebody_elses_mentor_profile(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The same gap on the other endpoint, also found by mutation.

    With no scope on the mentor-profile query, the first profile in the table is
    returned to whoever asks — so a mentee sees a stranger's headline and
    listing status. A test where nobody else is a mentor cannot see it.
    """
    auth_id = uuid4()
    mentee = await make_user(db_engine, auth_id, "mentee@example.com")
    mentor = await make_user(db_engine, uuid4(), "mentor@example.com")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO mentor_profiles (user_id, headline) VALUES (:u, 'Not yours')"),
            {"u": mentor},
        )

    response = await api_client.get(
        f"/api/v1/users/{mentee}/mentor-profile", headers=bearer(api_token(auth_id))
    )

    assert response.status_code == 404
    assert "Not yours" not in response.text


async def test_a_user_sees_only_their_own_goals(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Same shape as the education case. Written because a mutation batch that
    *claimed* to cover goals was matching no test at all — pytest exits non-zero
    for "no tests ran", which reads identically to "the mutant was caught"."""
    auth_id = uuid4()
    mine = await make_user(db_engine, auth_id, "self@example.com")
    theirs = await make_user(db_engine, uuid4(), "other@example.com")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO mentee_goals (user_id, notes) VALUES (:m, 'Mine'), (:t, 'Theirs')"),
            {"m": mine, "t": theirs},
        )

    body = (
        await api_client.get(f"/api/v1/users/{mine}/goals", headers=bearer(api_token(auth_id)))
    ).json()

    assert [g["notes"] for g in body["data"]] == ["Mine"]


async def test_goal_satellites_are_scoped_to_their_own_user(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`mentee_goal_countries` and `mentee_goal_needs` key on **user_id**, not on
    a goal id — the legacy shape, kept deliberately. That makes each one its own
    separately-scoped query, and a mutation showed the country scope was
    unexercised: every other goal test has only one user with countries, so
    dropping the filter changed nothing visible.

    Two users with satellites is what makes it visible.
    """
    auth_id = uuid4()
    mine = await make_user(db_engine, auth_id, "self@example.com")
    theirs = await make_user(db_engine, uuid4(), "other@example.com")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO mentee_goals (user_id, notes) VALUES (:m, 'Mine'), (:t, 'Theirs')"),
            {"m": mine, "t": theirs},
        )
        await conn.execute(
            text(
                "INSERT INTO mentee_goal_countries (user_id, country_id, priority) "
                "SELECT :m, id, 1 FROM countries WHERE code = 'NG'"
            ),
            {"m": mine},
        )
        await conn.execute(
            text(
                "INSERT INTO mentee_goal_countries (user_id, country_id, priority) "
                "SELECT :t, id, 1 FROM countries WHERE code = 'GB'"
            ),
            {"t": theirs},
        )
        await conn.execute(
            text(
                "INSERT INTO mentee_goal_needs (user_id, service_offering_id) "
                "SELECT :t, id FROM service_offerings ORDER BY sort_order LIMIT 1"
            ),
            {"t": theirs},
        )

    goal = (
        await api_client.get(f"/api/v1/users/{mine}/goals", headers=bearer(api_token(auth_id)))
    ).json()["data"][0]

    assert [c["code"] for c in goal["countries"]] == ["NG"]
    # The other user's need must not appear on this user's goal.
    assert goal["needs"] == []


async def test_a_user_sees_only_their_own_awards(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    mine = await make_user(db_engine, auth_id, "self@example.com")
    theirs = await make_user(db_engine, uuid4(), "other@example.com")
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO user_awards (user_id, institution, title) "
                "VALUES (:m, 'A Body', 'Mine'), (:t, 'A Body', 'Theirs')"
            ),
            {"m": mine, "t": theirs},
        )

    body = (
        await api_client.get(f"/api/v1/users/{mine}/awards", headers=bearer(api_token(auth_id)))
    ).json()

    assert [a["title"] for a in body["data"]] == ["Mine"]


# --------------------------------------------------------------------------
# The entity read follows the foreign key — no status filter
# --------------------------------------------------------------------------


async def test_a_pending_institution_still_renders_on_its_creators_profile(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The bug this PR is most likely to introduce.**

    Search excludes `pending_review` so nobody else selects an unvetted
    duplicate. Copying that filter into this join would blank the school on the
    profile of the person who typed it — the one user for whom the row is real,
    and the one waiting on the review.
    """
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "creator@example.com")
    async with db_engine.begin() as conn:
        institution = (
            await conn.execute(
                text(
                    "INSERT INTO institutions (name, source, status, created_by) "
                    "VALUES ('Unlisted Polytechnic', 'manual', "
                    "CAST('pending_review' AS lookup_status), :u) RETURNING id"
                ),
                {"u": user_id},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO education_entries (user_id, school_name_raw, institution_id) "
                "VALUES (:u, 'Unlisted Polytechnic', :i)"
            ),
            {"u": user_id, "i": institution},
        )

    body = (
        await api_client.get(
            f"/api/v1/users/{user_id}/education", headers=bearer(api_token(auth_id))
        )
    ).json()

    assert body["data"][0]["institution"]["name"] == "Unlisted Polytechnic"


async def test_an_unmatched_entry_keeps_its_raw_name(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`institution` null, `school_name_raw` intact — what makes an incomplete
    catalogue survivable rather than lossy (ADR 0008 point 5)."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "self@example.com")
    await add_education(db_engine, user_id, "Some School We Do Not Hold")

    entry = (
        await api_client.get(
            f"/api/v1/users/{user_id}/education", headers=bearer(api_token(auth_id))
        )
    ).json()["data"][0]

    assert entry["institution"] is None
    assert entry["school_name_raw"] == "Some School We Do Not Hold"


async def test_a_soft_deleted_education_entry_is_not_returned(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "self@example.com")
    await add_education(db_engine, user_id, "Deleted Degree")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE education_entries SET deleted_at = now() WHERE user_id = :u"),
            {"u": user_id},
        )

    body = (
        await api_client.get(
            f"/api/v1/users/{user_id}/education", headers=bearer(api_token(auth_id))
        )
    ).json()

    assert body["data"] == []


# --------------------------------------------------------------------------
# Mentor profile presence
# --------------------------------------------------------------------------


async def test_a_non_mentor_has_no_mentor_profile(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """404, not an empty object — which would claim they are a mentor with
    nothing filled in."""
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "mentee@example.com")

    response = await api_client.get(
        f"/api/v1/users/{user_id}/mentor-profile", headers=bearer(api_token(auth_id))
    )

    assert response.status_code == 404


async def test_a_mentor_profile_comes_back_with_its_offerings(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "mentor@example.com")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO mentor_profiles (user_id, headline) VALUES (:u, 'I help')"),
            {"u": user_id},
        )
        await conn.execute(
            text(
                "INSERT INTO mentor_service_offerings (mentor_user_id, service_offering_id) "
                "SELECT :u, id FROM service_offerings ORDER BY sort_order LIMIT 2"
            ),
            {"u": user_id},
        )
        # A second mentor, with a *different* offering. Without them, dropping
        # the `mentor_user_id` filter on the offerings query changes nothing
        # observable — a mutation proved exactly that, and this is the seed that
        # makes the scope real.
        other = await make_user(db_engine, uuid4(), "other-mentor@example.com")
        await conn.execute(
            text("INSERT INTO mentor_profiles (user_id, headline) VALUES (:u, 'Also helps')"),
            {"u": other},
        )
        await conn.execute(
            text(
                "INSERT INTO mentor_service_offerings (mentor_user_id, service_offering_id) "
                "SELECT :u, id FROM service_offerings ORDER BY sort_order DESC LIMIT 1"
            ),
            {"u": other},
        )

    body = (
        await api_client.get(
            f"/api/v1/users/{user_id}/mentor-profile", headers=bearer(api_token(auth_id))
        )
    ).json()

    assert body["headline"] == "I help"
    assert len(body["offerings"]) == 2, "another mentor's offerings leaked in"


# --------------------------------------------------------------------------
# /me and the sub-resources are one implementation
# --------------------------------------------------------------------------


async def test_me_and_the_sub_resource_agree(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The test that keeps two code paths from becoming two shapes.

    `/me` embeds education for the one-call profile render; `/users/{id}/education`
    serves the admin and the owner. They call the same store function and the
    same response model, and this fails the moment somebody re-implements one.
    """
    auth_id = uuid4()
    user_id = await make_user(db_engine, auth_id, "self@example.com")
    await add_education(db_engine, user_id, "Shared University")

    me = (await api_client.get("/api/v1/me", headers=bearer(api_token(auth_id)))).json()
    sub = (
        await api_client.get(
            f"/api/v1/users/{user_id}/education", headers=bearer(api_token(auth_id))
        )
    ).json()

    assert me["education"] == sub["data"]


async def test_me_still_carries_everything_it_did_before(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The embeds are **additive**. A client built against the previous shape
    must be unaffected, so every field it relied on is asserted present."""
    auth_id = uuid4()
    await make_user(db_engine, auth_id, "self@example.com")

    me = (await api_client.get("/api/v1/me", headers=bearer(api_token(auth_id)))).json()

    for field in ("id", "email", "first_name", "primary_role", "timezone", "created_at"):
        assert field in me, f"/me lost {field}"
    assert me["education"] == []
    assert me["mentor_profile"] is None
