"""The session endpoints: refusals first, then behaviour.

**Refusals first because this is the phase's authorization surface.** Four prior
M4 pull requests each honestly answered "no read surface ships here", and every
one of those deferrals lands on these five paths.

The case that matters most is **a session the caller is not part of, by id**.
The dependency cannot help there — `/sessions/{id}` names no user for
`TargetUserDep` to resolve — so the `WHERE` clause in the store is the only
thing standing between a caller and somebody else's booking message. A mutation
batch found nine endpoints in M2 whose refusal tests all stopped at the
dependency and never exercised that clause.

``session_events`` is the sharpest of the five: it carries no party columns at
all, so filtering on ``session_id`` alone would look scoped and return any
session's history to anyone holding an id.

The asymmetry here runs the other way from the profile routes. An admin may list
**a user's** sessions — that URL names whose records are being reviewed — and may
**not** read a single session by id, because that URL names nobody.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

STARTS_AT = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)


async def make_user(
    engine: AsyncEngine, auth_id: UUID, email: str, *, mentor: bool = False, admin: bool = False
) -> UUID:
    async with engine.begin() as conn:
        user_id = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
                    "VALUES (:e, :a, 'Ada', :r, 'Africa/Lagos') RETURNING id"
                ),
                {"e": email, "a": auth_id, "r": "mentor" if mentor else "mentee"},
            )
        ).scalar_one()
        if mentor:
            await conn.execute(
                text("INSERT INTO mentor_profiles (user_id, headline) VALUES (:u, 'M')"),
                {"u": user_id},
            )
        if admin:
            await conn.execute(
                text("INSERT INTO admin_users (user_id, admin_role) VALUES (:u, 'super_admin')"),
                {"u": user_id},
            )
    return user_id


async def make_session(
    engine: AsyncEngine,
    mentor_id: UUID,
    mentee_id: UUID,
    *,
    status: str = "cancelled",
    starts_at: datetime = STARTS_AT,
    message: str | None = "please help with my SOP",
    with_event: bool = False,
) -> UUID:
    async with engine.begin() as conn:
        session_id = (
            await conn.execute(
                text(
                    "INSERT INTO sessions (mentor_id, mentee_id, status, starts_at, "
                    "duration_minutes, booking_message) "
                    "VALUES (:m, :e, :s, :t, 45, :b) RETURNING id"
                ),
                {"m": mentor_id, "e": mentee_id, "s": status, "t": starts_at, "b": message},
            )
        ).scalar_one()
        if with_event:
            await conn.execute(
                text(
                    "INSERT INTO session_events (session_id, to_status, actor_type) "
                    "VALUES (:s, 'cancelled', "
                    "'system')"
                ),
                {"s": session_id},
            )
    return session_id


def sessions_url(user_id: UUID) -> str:
    return f"/api/v1/users/{user_id}/sessions"


async def pair(engine: AsyncEngine, tag: str) -> tuple[UUID, UUID, UUID, UUID]:
    """A mentor and a mentee, with their auth ids."""
    mentor_auth, mentee_auth = uuid4(), uuid4()
    mentor = await make_user(engine, mentor_auth, f"mentor-{tag}@example.test", mentor=True)
    mentee = await make_user(engine, mentee_auth, f"mentee-{tag}@example.test")
    return mentor, mentor_auth, mentee, mentee_auth


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------

#: Every read path. An endpoint added without a refusal test is a line missing
#: from here, which is visible in a diff.
PATHS = ["list", "detail", "events"]


@pytest.mark.parametrize("kind", PATHS)
async def test_a_read_without_a_token_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, kind: str
) -> None:
    mentor, _, mentee, _ = await pair(db_engine, f"notoken-{kind}")
    session_id = await make_session(db_engine, mentor, mentee)
    path = {
        "list": sessions_url(mentee),
        "detail": f"/api/v1/sessions/{session_id}",
        "events": f"/api/v1/sessions/{session_id}/events",
    }[kind]

    response = await api_client.get(path)

    assert response.status_code == 401


async def test_another_users_session_list_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, _, mentee, _ = await pair(db_engine, "list-intruder")
    intruder_auth = uuid4()
    await make_user(db_engine, intruder_auth, "intruder-list@example.test")

    response = await api_client.get(sessions_url(mentee), headers=bearer(api_token(intruder_auth)))

    assert response.status_code == 404


@pytest.mark.parametrize("suffix", ["", "/events"])
async def test_a_session_the_caller_is_not_part_of_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, suffix: str
) -> None:
    """The case the dependency cannot catch.

    `/sessions/{id}` names no user, so `TargetUserDep` never runs. The `WHERE`
    clause in the store is the whole of the control.
    """
    mentor, _, mentee, _ = await pair(db_engine, f"outsider{suffix.strip('/')}")
    session_id = await make_session(db_engine, mentor, mentee, with_event=True)
    outsider_auth = uuid4()
    await make_user(db_engine, outsider_auth, f"outsider{suffix.strip('/')}@example.test")

    response = await api_client.get(
        f"/api/v1/sessions/{session_id}{suffix}", headers=bearer(api_token(outsider_auth))
    )

    assert response.status_code == 404


@pytest.mark.parametrize("suffix", ["", "/events"])
async def test_an_admin_may_not_read_a_session_by_id(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, suffix: str
) -> None:
    """Deliberately narrower than the list endpoint.

    `/sessions/{id}` names no user whose records an admin could be said to be
    reviewing. Widening later is additive; narrowing after a client has built
    against it is not.
    """
    mentor, _, mentee, _ = await pair(db_engine, f"adminid{suffix.strip('/')}")
    session_id = await make_session(db_engine, mentor, mentee, with_event=True)
    admin_auth = uuid4()
    await make_user(db_engine, admin_auth, f"admin{suffix.strip('/')}@example.test", admin=True)

    response = await api_client.get(
        f"/api/v1/sessions/{session_id}{suffix}", headers=bearer(api_token(admin_auth))
    )

    assert response.status_code == 404


async def test_events_for_another_pairs_session_are_not_an_empty_list(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """404, never `[]`.

    An empty list asserts "this session exists and has no history", which is a
    different claim and leaks the session's existence. The store returns `None`
    for "not yours" precisely so the route can tell them apart.
    """
    mentor, _, mentee, _ = await pair(db_engine, "emptyleak")
    session_id = await make_session(db_engine, mentor, mentee, with_event=True)
    outsider_auth = uuid4()
    await make_user(db_engine, outsider_auth, "outsider-empty@example.test")

    response = await api_client.get(
        f"/api/v1/sessions/{session_id}/events", headers=bearer(api_token(outsider_auth))
    )

    assert response.status_code == 404
    assert response.json() != []


# --------------------------------------------------------------------------
# Behaviour
# --------------------------------------------------------------------------


@pytest.mark.parametrize("who", ["mentor", "mentee"])
async def test_both_parties_may_read_the_session(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, who: str
) -> None:
    mentor, mentor_auth, mentee, mentee_auth = await pair(db_engine, f"both-{who}")
    session_id = await make_session(db_engine, mentor, mentee)
    auth = mentor_auth if who == "mentor" else mentee_auth

    response = await api_client.get(
        f"/api/v1/sessions/{session_id}", headers=bearer(api_token(auth))
    )

    assert response.status_code == 200
    body = response.json()
    assert body["mentor_id"] == str(mentor)
    assert body["mentee_id"] == str(mentee)
    assert body["booking_message"] == "please help with my SOP"


async def test_an_admin_may_list_another_users_sessions(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The URL names whose records are being reviewed, so an admin is admitted —
    the same asymmetry the profile routes have."""
    mentor, _, mentee, _ = await pair(db_engine, "adminlist")
    await make_session(db_engine, mentor, mentee)
    admin_auth = uuid4()
    await make_user(db_engine, admin_auth, "admin-list@example.test", admin=True)

    response = await api_client.get(sessions_url(mentee), headers=bearer(api_token(admin_auth)))

    assert response.status_code == 200
    assert len(response.json()["data"]) == 1


async def test_the_list_contains_only_the_users_own_sessions(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The test that reaches the `WHERE`, rather than stopping at the dependency.

    ``test_another_users_session_list_is_refused`` is refused by
    ``TargetUserDep`` before the store runs, so it proves nothing about the
    store's filter — deleting `_is_a_party` from the list query left it green.
    That is the exact failure M2 shipped nine times, and the module docstring
    claiming otherwise did not prevent it here either.

    Here the dependency **passes**: the caller is asking for their own sessions.
    The only thing between them and a stranger's booking message is the
    predicate in the query.
    """
    mine_mentor, _, me, my_auth = await pair(db_engine, "onlymine")
    await make_session(db_engine, mine_mentor, me, message="mine")

    other_mentor, _, other_mentee, _ = await pair(db_engine, "onlytheirs")
    await make_session(db_engine, other_mentor, other_mentee, message="theirs")

    response = await api_client.get(sessions_url(me), headers=bearer(api_token(my_auth)))

    assert response.status_code == 200
    rows = response.json()["data"]
    assert len(rows) == 1
    assert rows[0]["booking_message"] == "mine"


async def test_a_cancelled_session_is_still_listed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """It is still that person's history. Omitting it would silently shorten it,
    and D20's profile-access rule says "any status" for the same reason."""
    mentor, _, mentee, mentee_auth = await pair(db_engine, "cancelled")
    await make_session(db_engine, mentor, mentee, status="cancelled")

    response = await api_client.get(sessions_url(mentee), headers=bearer(api_token(mentee_auth)))

    assert response.status_code == 200
    assert [row["status"] for row in response.json()["data"]] == ["cancelled"]


async def test_a_user_with_no_sessions_gets_an_empty_page(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """200, not 404. An empty collection exists; it just holds nothing.

    A 404 would leave a client unable to tell "nothing yet" from "refused", and
    the second is a permission signal deliberately hidden.
    """
    auth = uuid4()
    user_id = await make_user(db_engine, auth, "lonely@example.test")

    response = await api_client.get(sessions_url(user_id), headers=bearer(api_token(auth)))

    assert response.status_code == 200
    assert response.json() == {"data": [], "next_cursor": None}


async def test_the_history_reads_oldest_first_and_renders_a_null_actor(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """A null actor means the sweep did it — a real state, not a missing value."""
    mentor, _, mentee, mentee_auth = await pair(db_engine, "history")
    session_id = await make_session(db_engine, mentor, mentee, with_event=True)

    response = await api_client.get(
        f"/api/v1/sessions/{session_id}/events", headers=bearer(api_token(mentee_auth))
    )

    assert response.status_code == 200
    body = response.json()
    assert body["next_cursor"] is None, "a bounded history is returned whole"
    events = body["data"]
    assert len(events) == 1
    assert events[0]["actor_id"] is None
    assert events[0]["actor_type"] == "system"
    assert events[0]["reason_code"] is None


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------


async def test_the_list_pages_and_the_cursor_round_trips(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor, _, mentee, mentee_auth = await pair(db_engine, "paging")
    for offset in range(3):
        await make_session(db_engine, mentor, mentee, starts_at=STARTS_AT + timedelta(days=offset))

    first = await api_client.get(
        f"{sessions_url(mentee)}?limit=2", headers=bearer(api_token(mentee_auth))
    )
    assert first.status_code == 200
    assert len(first.json()["data"]) == 2
    cursor = first.json()["next_cursor"]
    assert cursor

    second = await api_client.get(
        f"{sessions_url(mentee)}?limit=2&cursor={cursor}",
        headers=bearer(api_token(mentee_auth)),
    )

    assert second.status_code == 200
    assert len(second.json()["data"]) == 1
    assert second.json()["next_cursor"] is None
    assert {row["id"] for row in first.json()["data"]} & {
        row["id"] for row in second.json()["data"]
    } == set()


async def test_sessions_sharing_a_start_time_do_not_straddle_a_page_boundary(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The tiebreak ADR 0016's amendment exists for.

    The cursor is `(starts_at, id)` rather than `starts_at` alone, because a
    sort key that repeats cannot say where a page ended. Three sessions at the
    **same instant** are legal — the double-booking constraint is partial on the
    live statuses, and these are cancelled — and paging two at a time must
    return each exactly once. Comparing on `starts_at` alone either skips the
    third or repeats the second, and both look like working software.

    The existing paging test seeds sessions a day apart, so every sort key is
    distinct and the id half of the cursor is never load-bearing.
    """
    mentor, _, mentee, mentee_auth = await pair(db_engine, "tiebreak")
    expected = {
        str(await make_session(db_engine, mentor, mentee, starts_at=STARTS_AT)) for _ in range(3)
    }

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(4):  # bounded, so a cursor that never advances fails rather than hangs
        query = f"{sessions_url(mentee)}?limit=2" + (f"&cursor={cursor}" if cursor else "")
        page = await api_client.get(query, headers=bearer(api_token(mentee_auth)))
        assert page.status_code == 200
        seen.extend(row["id"] for row in page.json()["data"])
        cursor = page.json()["next_cursor"]
        if cursor is None:
            break

    assert cursor is None, "paging never terminated"
    assert len(seen) == len(set(seen)), "a session was returned on two pages"
    assert set(seen) == expected, "a session was skipped across the page boundary"


async def test_a_malformed_cursor_is_a_client_error(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    _, _, mentee, mentee_auth = await pair(db_engine, "badcursor")

    response = await api_client.get(
        f"{sessions_url(mentee)}?cursor=not-a-cursor", headers=bearer(api_token(mentee_auth))
    )

    assert response.status_code == 422


async def test_a_cursor_whose_sort_key_is_not_a_timestamp_is_a_client_error(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The 500 this would otherwise have been.

    `decode_cursor` only checks that the token is base64 holding a UUID, so a
    well-formed token carrying a nonsense sort key reaches the store — where
    `fromisoformat` would raise `ValueError` and surface as a server error for
    what is plainly a client mistake.
    """
    _, _, mentee, mentee_auth = await pair(db_engine, "badsortkey")
    forged = base64.urlsafe_b64encode(f"banana\x00{uuid4()}".encode()).decode()

    response = await api_client.get(
        f"{sessions_url(mentee)}?cursor={forged}", headers=bearer(api_token(mentee_auth))
    )

    assert response.status_code == 422


# --------------------------------------------------------------------------
# Who the other party is
#
# `mentor_id` and `mentee_id` shipped as bare UUIDs, so a mentee's own session
# list could not say who the session was with — for any mentor, listed or not.
# Nothing caught it because no client existed to be disappointed.
# --------------------------------------------------------------------------


async def give_profile(engine: AsyncEngine, user_id: UUID, avatar: str | None) -> None:
    """A `user_profiles` row, with or without an avatar.

    `make_user` creates none, so a user without this call exercises the *missing
    row* cause of a null avatar, and a user with `avatar=None` exercises the
    *null column* cause. Two different joins fail two different ways and the
    client sees one null, which is why both need their own test.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO user_profiles (user_id, avatar_url) VALUES (:u, :a)"),
            {"u": user_id, "a": avatar},
        )


@pytest.mark.parametrize("who", ["mentor", "mentee"])
async def test_both_parties_are_named_on_the_detail(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, who: str
) -> None:
    mentor, mentor_auth, mentee, mentee_auth = await pair(db_engine, f"named-{who}")
    await give_profile(db_engine, mentor, "https://cdn.test/m.png")
    session_id = await make_session(db_engine, mentor, mentee)
    auth = mentor_auth if who == "mentor" else mentee_auth

    body = (
        await api_client.get(f"/api/v1/sessions/{session_id}", headers=bearer(api_token(auth)))
    ).json()

    assert body["mentor"]["id"] == str(mentor)
    assert body["mentor"]["first_name"] == "Ada"
    assert body["mentor"]["avatar_url"] == "https://cdn.test/m.png"
    assert body["mentee"]["id"] == str(mentee)
    # The bare ids are still there — removing them would be the breaking change.
    assert body["mentor_id"] == str(mentor)


async def test_the_list_names_both_parties_too(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """One shared column tuple, so the list and the detail cannot disagree."""
    mentor, _, mentee, mentee_auth = await pair(db_engine, "named-list")
    await make_session(db_engine, mentor, mentee)

    rows = (
        await api_client.get(sessions_url(mentee), headers=bearer(api_token(mentee_auth)))
    ).json()["data"]

    assert rows[0]["mentor"]["first_name"] == "Ada"
    assert rows[0]["mentee"]["id"] == str(mentee)


async def test_a_party_with_no_profile_row_still_returns_the_session(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The join is outer, and this is why.

    `user_profiles` is where `avatar_url` lives, and a user who never filled in
    a profile has no row at all. An inner join would drop the session from
    **both** parties' lists — data loss wearing a display bug's clothes.
    """
    mentor, _, mentee, mentee_auth = await pair(db_engine, "no-profile")
    session_id = await make_session(db_engine, mentor, mentee)

    body = (
        await api_client.get(
            f"/api/v1/sessions/{session_id}", headers=bearer(api_token(mentee_auth))
        )
    ).json()

    assert body["id"] == str(session_id)
    assert body["mentor"]["avatar_url"] is None
    assert body["mentor"]["first_name"] == "Ada"


async def test_a_party_with_a_profile_but_no_avatar_is_the_other_null(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Same null, different cause.

    The test above loses the whole row; this one loses only the column. An inner
    join would pass this one while failing that, which is how one of the two
    causes ends up untested.
    """
    mentor, _, mentee, mentee_auth = await pair(db_engine, "null-avatar")
    await give_profile(db_engine, mentor, None)
    session_id = await make_session(db_engine, mentor, mentee)

    body = (
        await api_client.get(
            f"/api/v1/sessions/{session_id}", headers=bearer(api_token(mentee_auth))
        )
    ).json()

    assert body["mentor"]["avatar_url"] is None
    assert body["id"] == str(session_id)


async def test_a_party_with_no_name_returns_nulls_rather_than_failing(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`users.first_name` is nullable and the M2 transform maps it from an
    optional Bubble field, so a nameless party is real data rather than an edge
    case somebody invented."""
    mentor, _, mentee, mentee_auth = await pair(db_engine, "nameless")
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET first_name = NULL, last_name = NULL WHERE id = :u"),
            {"u": mentor},
        )
    session_id = await make_session(db_engine, mentor, mentee)

    body = (
        await api_client.get(
            f"/api/v1/sessions/{session_id}", headers=bearer(api_token(mentee_auth))
        )
    ).json()

    assert body["mentor"]["first_name"] is None
    assert body["mentor"]["last_name"] is None
    assert body["mentor"]["id"] == str(mentor)


async def test_a_soft_deleted_party_is_still_named_in_their_history(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The **absence** of a `deleted_at` predicate, asserted so it is visible.

    This is the opposite of the public endpoints, where a deleted mentor
    disappears. Here the authorization is the session itself: the mentee had a
    real session with a real person, and their own record must not decay into a
    UUID because that person later left the platform.

    Without this test, a future "tidy up: add the soft-delete predicate
    everywhere" sweep would silently break history and look like an improvement.
    """
    mentor, _, mentee, mentee_auth = await pair(db_engine, "gone")
    session_id = await make_session(db_engine, mentor, mentee)
    async with db_engine.begin() as conn:
        await conn.execute(text("UPDATE users SET deleted_at = now() WHERE id = :u"), {"u": mentor})

    body = (
        await api_client.get(
            f"/api/v1/sessions/{session_id}", headers=bearer(api_token(mentee_auth))
        )
    ).json()

    assert body["mentor"]["first_name"] == "Ada"


async def test_the_joins_do_not_multiply_rows(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Four joins on a keyset-paged query is where a duplicate row appears.

    Every join is many-to-one — a primary key or a unique `user_id` — so none
    can multiply. Asserted rather than reasoned, because a duplicate would not
    merely repeat a name: it would consume the page limit and shift the cursor,
    losing rows silently at the boundary.
    """
    mentor, _, mentee, mentee_auth = await pair(db_engine, "nodup")
    await give_profile(db_engine, mentor, "https://cdn.test/m.png")
    await give_profile(db_engine, mentee, "https://cdn.test/e.png")
    for offset in range(3):
        await make_session(db_engine, mentor, mentee, starts_at=STARTS_AT + timedelta(days=offset))

    rows = (
        await api_client.get(sessions_url(mentee), headers=bearer(api_token(mentee_auth)))
    ).json()["data"]

    assert len(rows) == 3
    assert len({row["id"] for row in rows}) == 3


async def test_no_party_email_reaches_the_response(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`users` carries email, `email_verified_at`, `slug` and `last_active_at`.

    The parties are meeting; that does not make the rest of each other's account
    their business.
    """
    mentor, _, mentee, mentee_auth = await pair(db_engine, "no-email")
    await make_session(db_engine, mentor, mentee)

    raw = (await api_client.get(sessions_url(mentee), headers=bearer(api_token(mentee_auth)))).text

    assert "mentor-no-email@example.test" not in raw
    assert "email" not in raw
