"""A mentor sees their own profile, whatever state it is in.

Product rule, 2026-09-27: anyone with a `mentor_profiles` row — pending,
declined, unlisted — who opens `/mentors/{their id or slug}` sees it, with
`approval_status` and `listing_status` added. Everyone else keeps the 404 that
says nothing about why, and the two status fields never reach them.

The profile also carries `next_available_*` now, with the card's semantics — and
a mentor strangers cannot see has nothing bookable, so it reads `none`.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import make_bookable_mentor, make_public_mentor

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]


def url(handle: object, tail: str = "") -> str:
    return f"/api/v1/mentors/{handle}{tail}"


async def auth_of(engine: AsyncEngine, user: UUID) -> dict[str, str]:
    async with engine.begin() as conn:
        auth_id = (
            await conn.execute(text("SELECT auth_id FROM users WHERE id = :u"), {"u": user})
        ).scalar_one()
    return bearer(api_token(auth_id))


async def a_stranger(engine: AsyncEngine, tag: str) -> dict[str, str]:
    auth_id = uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (email, auth_id, primary_role, timezone) "
                "VALUES (:e, :a, 'mentee', 'UTC')"
            ),
            {"e": f"stranger-{tag}@example.test", "a": auth_id},
        )
    return bearer(api_token(auth_id))


async def set_status(engine: AsyncEngine, mentor: UUID, **columns: str) -> None:
    sets = ", ".join(f"{k} = :{k}" for k in columns)
    async with engine.begin() as conn:
        await conn.execute(
            text(f"UPDATE mentor_profiles SET {sets} WHERE user_id = :u"),  # noqa: S608
            {"u": mentor, **columns},
        )


@pytest.mark.parametrize("by", ["id", "slug"])
@pytest.mark.parametrize(
    ("state", "knob", "approval", "listing"),
    [
        ("pending", {"approved": False}, "pending", "listed"),
        ("unlisted", {"listed": False}, "approved", "unlisted"),
    ],
)
async def test_the_owner_sees_their_profile_in_any_state(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    by: str,
    state: str,
    knob: dict[str, bool],
    approval: str,
    listing: str,
) -> None:
    slug = f"owner-{state}-{by}"
    mentor = await make_public_mentor(db_engine, slug, slug=slug, **knob)

    response = await api_client.get(
        url(mentor if by == "id" else slug), headers=await auth_of(db_engine, mentor)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(mentor)
    assert body["approval_status"] == approval
    assert body["listing_status"] == listing


async def test_a_declined_owner_still_sees_their_profile(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "owner-declined", approved=False)
    await set_status(db_engine, mentor, approval_status="declined")

    response = await api_client.get(url(mentor), headers=await auth_of(db_engine, mentor))

    assert response.status_code == 200
    assert response.json()["approval_status"] == "declined"


async def test_another_signed_in_user_still_gets_the_404(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "owner-hidden", approved=False)

    response = await api_client.get(
        url(mentor), headers=await a_stranger(db_engine, "owner-hidden")
    )

    assert response.status_code == 404


async def test_the_owner_of_a_deleted_profile_gets_the_404(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "owner-deleted")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE mentor_profiles SET deleted_at = now() WHERE user_id = :u"),
            {"u": mentor},
        )

    response = await api_client.get(url(mentor), headers=await auth_of(db_engine, mentor))

    assert response.status_code == 404


async def test_the_statuses_reach_the_owner_and_nobody_else(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Absent rather than null for everyone else, so their presence *means*
    "you are the owner" — the allowlist test in `test_api_mentors` asserts the
    anonymous half, this asserts a signed-in stranger's."""
    mentor = await make_public_mentor(db_engine, "owner-public")

    theirs = (
        await api_client.get(url(mentor), headers=await a_stranger(db_engine, "owner-public"))
    ).json()
    mine = (await api_client.get(url(mentor), headers=await auth_of(db_engine, mentor))).json()

    assert "approval_status" not in theirs
    assert "listing_status" not in theirs
    assert mine["approval_status"] == "approved"
    assert mine["listing_status"] == "listed"


async def test_a_response_that_depends_on_the_token_is_private(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`Vary` on every response, since any of them could have differed by
    caller; `private` whenever a token was sent, so no shared cache keeps an
    owner's view."""
    mentor = await make_public_mentor(db_engine, "owner-cache")

    anonymous = await api_client.get(url(mentor))
    signed_in = await api_client.get(url(mentor), headers=await auth_of(db_engine, mentor))

    assert "authorization" in anonymous.headers["vary"].lower()
    assert "private" not in anonymous.headers.get("cache-control", "")
    assert "authorization" in signed_in.headers["vary"].lower()
    assert "private" in signed_in.headers["cache-control"]


async def test_the_owner_reads_their_own_reviews_list(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "owner-reviews", listed=False)

    mine = await api_client.get(url(mentor, "/reviews"), headers=await auth_of(db_engine, mentor))
    theirs = await api_client.get(
        url(mentor, "/reviews"), headers=await a_stranger(db_engine, "owner-reviews")
    )

    assert mine.status_code == 200
    assert mine.headers["cache-control"].startswith("private")
    assert theirs.status_code == 404


async def test_the_profile_carries_the_cards_next_available_time(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "profile-next")
    at = dt.datetime(2030, 1, 7, 9, tzinfo=dt.UTC)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO mentor_next_availability "
                "(mentor_user_id, next_available_at, bookable_until, computed_at) "
                "VALUES (:u, :at, :until, now())"
            ),
            {"u": mentor, "at": at, "until": at - dt.timedelta(hours=1)},
        )
        await conn.execute(
            text("DELETE FROM mentor_availability_changes WHERE mentor_user_id = :u"),
            {"u": mentor},
        )

    body = (await api_client.get(url(mentor))).json()

    assert body["next_available_state"] == "open"
    assert dt.datetime.fromisoformat(body["next_available_at"]) == at


async def test_a_mentor_the_job_has_not_reached_reads_refreshing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "profile-refreshing")

    body = (await api_client.get(url(mentor))).json()

    assert body["next_available_state"] == "refreshing"
    assert body["next_available_at"] is None


async def test_a_hidden_profile_has_nothing_bookable(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Strangers cannot book a mentor they cannot see, so the owner's preview
    says `none` — not `refreshing`, which would promise a time that never comes,
    since the job only refreshes bookable mentors."""
    mentor = await make_public_mentor(db_engine, "owner-next", approved=False)

    body = (await api_client.get(url(mentor), headers=await auth_of(db_engine, mentor))).json()

    assert body["next_available_state"] == "none"
    assert body["next_available_at"] is None
    assert body["session_types"] == []


async def test_a_stored_time_is_withheld_once_it_is_stale(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A change since the job ran makes the stored time a stale claim: the
    state says `refreshing` and the time must not be sent beside it."""
    mentor = await make_bookable_mentor(db_engine, "profile-stale")
    at = dt.datetime(2030, 1, 7, 9, tzinfo=dt.UTC)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO mentor_next_availability "
                "(mentor_user_id, next_available_at, bookable_until, computed_at) "
                "VALUES (:u, :at, :until, now())"
            ),
            {"u": mentor, "at": at, "until": at - dt.timedelta(hours=1)},
        )
        await conn.execute(
            text("INSERT INTO mentor_availability_changes (mentor_user_id) VALUES (:u)"),
            {"u": mentor},
        )

    body = (await api_client.get(url(mentor))).json()

    assert body["next_available_state"] == "refreshing"
    assert body["next_available_at"] is None


async def store_future_time(engine: AsyncEngine, mentor: UUID) -> None:
    """A vouched, future time with no change since — what `open` is made of."""
    at = dt.datetime(2030, 1, 7, 9, tzinfo=dt.UTC)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO mentor_next_availability "
                "(mentor_user_id, next_available_at, bookable_until, computed_at) "
                "VALUES (:u, :at, :until, now())"
            ),
            {"u": mentor, "at": at, "until": at - dt.timedelta(hours=1)},
        )
        await conn.execute(
            text("DELETE FROM mentor_availability_changes WHERE mentor_user_id = :u"),
            {"u": mentor},
        )


async def test_a_visible_mentor_nobody_can_book_shows_no_time(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Approved and listed, but no offering or hours: the job does not cover
    them, so a row left from when they were bookable is no longer watched and
    would read `open` for a mentor nobody can book."""
    mentor = await make_public_mentor(db_engine, "visible-unbookable")
    await store_future_time(db_engine, mentor)

    # Hidden from strangers since #15, so asked as the owner, who still reads it.
    body = (await api_client.get(url(mentor), headers=await auth_of(db_engine, mentor))).json()

    assert body["next_available_state"] == "none"
    assert body["next_available_at"] is None


async def test_a_bad_token_on_a_public_profile_reads_as_anonymous(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A tab resumed with an expired token must not break a page anyone may
    read. The token can only add the owner's view, never take the public one."""
    mentor = await make_bookable_mentor(db_engine, "bad-token")

    response = await api_client.get(url(mentor), headers={"Authorization": "Bearer not-a-jwt"})

    assert response.status_code == 200
    assert "approval_status" not in response.json()
    assert "private" in response.headers["cache-control"]


async def test_a_deleted_accounts_token_reads_as_anonymous(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_bookable_mentor(db_engine, "gone-viewer-target")
    headers = await a_stranger(db_engine, "gone-viewer")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET deleted_at = now() WHERE email = :e"),
            {"e": "stranger-gone-viewer@example.test"},
        )

    response = await api_client.get(url(mentor), headers=headers)

    assert response.status_code == 200
    assert "approval_status" not in response.json()


async def test_the_owner_of_a_deleted_account_gets_the_404(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The *user* row this time — soft deletion is waived for the owner on
    neither table."""
    mentor = await make_public_mentor(db_engine, "owner-user-deleted", approved=False)
    headers = await auth_of(db_engine, mentor)
    async with db_engine.begin() as conn:
        await conn.execute(text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": mentor})

    response = await api_client.get(url(mentor), headers=headers)

    assert response.status_code == 404


async def test_a_404_carries_the_cache_headers_too(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Raised in a dependency, so no route code runs — yet a shared cache holding
    the anonymous 404 must not serve it to the owner."""
    mentor = await make_public_mentor(db_engine, "hidden-404", approved=False)

    anonymous = await api_client.get(url(mentor))
    signed_in = await api_client.get(url(mentor), headers=await a_stranger(db_engine, "hidden-404"))

    assert anonymous.status_code == signed_in.status_code == 404
    assert "authorization" in anonymous.headers["vary"].lower()
    assert "private" in signed_in.headers["cache-control"]


async def test_the_published_contract_keeps_every_profile_field(
    api_client: httpx.AsyncClient,
) -> None:
    """The frontend generates its client from this document. A serializer that
    erased the schema would turn the whole profile into an untyped object."""
    spec = (await api_client.get("/openapi.json")).json()
    profile = spec["components"]["schemas"]["MentorPublicRead"]["properties"]
    operation = spec["paths"]["/api/v1/mentors/{handle}"]["get"]

    assert {
        "id",
        "session_types",
        "approval_status",
        "listing_status",
        "next_available_state",
    } <= set(profile)
    assert {} in operation["security"], "the token is optional, not required"
