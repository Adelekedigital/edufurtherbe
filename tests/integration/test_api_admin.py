"""The review surface, and who may reach it.

**Every other test file protects a user's rows from other users. This one
protects an action from callers who may not take it** — the control is the
caller's grant, not a row scope, so each endpoint has four cases rather than
two: no token, a user with no grant, a *revoked* admin, and a live one.

The revoked case is the one that has been missed before. `revoked_at` is what
makes revocation revoke, and an admin check that reads only whether a grant row
exists passes every other test in this file.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

ADMIN = "/api/v1/admin"


async def make_user(
    engine: AsyncEngine,
    auth_id: UUID,
    email: str,
    *,
    role: str | None = None,
    revoked: bool = False,
) -> UUID:
    async with engine.begin() as conn:
        user_id = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
                    "VALUES (:e, :a, 'Ada', 'mentee', 'UTC') RETURNING id"
                ),
                {"e": email, "a": auth_id},
            )
        ).scalar_one()
        if role:
            await conn.execute(
                text(
                    "INSERT INTO admin_users (user_id, admin_role, revoked_at) "
                    "VALUES (:u, :r, now())"
                    if revoked
                    else "INSERT INTO admin_users (user_id, admin_role) VALUES (:u, :r)"
                ),
                {"u": user_id, "r": role},
            )
    return user_id


async def add_institution(
    engine: AsyncEngine, name: str, *, status: str = "pending_review", creator: UUID | None = None
) -> UUID:
    async with engine.begin() as conn:
        if creator is None:
            creator = (
                await conn.execute(
                    text(
                        "INSERT INTO users (email, primary_role, timezone) "
                        "VALUES (:e, 'mentee', 'UTC') RETURNING id"
                    ),
                    {"e": f"creator-{uuid4()}@example.com"},
                )
            ).scalar_one()
        return (
            await conn.execute(
                text(
                    "INSERT INTO institutions (name, source, status, created_by) "
                    "VALUES (:n, 'manual', CAST(:s AS lookup_status), :b) RETURNING id"
                ),
                {"n": name, "s": status, "b": creator},
            )
        ).scalar_one()


async def add_education(engine: AsyncEngine, user_id: UUID, institution_id: UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO education_entries (user_id, school_name_raw, institution_id) "
                "VALUES (:u, 'typed', :i)"
            ),
            {"u": user_id, "i": institution_id},
        )


async def add_mentor(engine: AsyncEngine, user_id: UUID, headline: str = "I help") -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO mentor_profiles (user_id, headline) VALUES (:u, :h)"),
            {"u": user_id, "h": headline},
        )


#: Every admin endpoint, so one added without an authorization test is a missing
#: line here rather than an invisible gap.
ENDPOINTS: list[tuple[str, str, dict[str, Any] | None]] = [
    ("get", "/institutions/pending", None),
    ("post", "/institutions/{id}/approve", None),
    ("post", "/institutions/{id}/merge", {"winning_id": str(uuid4())}),
    ("get", "/mentors/pending", None),
    ("post", "/mentors/{id}/decision", {"reason": None}),
]


# --------------------------------------------------------------------------
# Who may reach this surface at all
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "body"), ENDPOINTS, ids=[p for _, p, _ in ENDPOINTS])
async def test_no_token_is_refused(
    api_client: httpx.AsyncClient, method: str, path: str, body: dict[str, Any] | None
) -> None:
    response = await api_client.request(
        method.upper(), ADMIN + path.replace("{id}", str(uuid4())), json=body
    )

    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path", "body"), ENDPOINTS, ids=[p for _, p, _ in ENDPOINTS])
async def test_a_user_with_no_grant_is_refused(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    auth_id = uuid4()
    await make_user(db_engine, auth_id, "nobody@example.com")

    response = await api_client.request(
        method.upper(),
        ADMIN + path.replace("{id}", str(uuid4())),
        json=body,
        headers=bearer(api_token(auth_id)),
    )

    assert response.status_code == 404, "an ordinary user reached the review surface"


@pytest.mark.parametrize(("method", "path", "body"), ENDPOINTS, ids=[p for _, p, _ in ENDPOINTS])
async def test_a_revoked_admin_is_refused(
    api_client: httpx.AsyncClient,
    db_engine: AsyncEngine,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    """`revoked_at` is what makes revocation revoke. A check reading only whether
    a grant row exists passes every other test in this file."""
    auth_id = uuid4()
    await make_user(db_engine, auth_id, "gone@example.com", role="super_admin", revoked=True)

    response = await api_client.request(
        method.upper(),
        ADMIN + path.replace("{id}", str(uuid4())),
        json=body,
        headers=bearer(api_token(auth_id)),
    )

    assert response.status_code == 404, "a revoked admin still reached the review surface"


async def test_a_refusal_is_404_not_403(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """403 would confirm the endpoint exists and that somebody may use it."""
    auth_id = uuid4()
    await make_user(db_engine, auth_id, "nobody@example.com")

    response = await api_client.get(
        f"{ADMIN}/institutions/pending", headers=bearer(api_token(auth_id))
    )

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")


# --------------------------------------------------------------------------
# Roles are not interchangeable
# --------------------------------------------------------------------------


async def test_mentor_approval_cannot_curate_the_catalogue(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`AdminRole` distinguishes what a grant is *for*. Treating every grant as
    equivalent would make the enum decorative, and this schema has removed a
    decorative column before."""
    auth_id = uuid4()
    await make_user(db_engine, auth_id, "mentors@example.com", role="mentor_approval")
    institution = await add_institution(db_engine, "Somewhere")

    response = await api_client.post(
        f"{ADMIN}/institutions/{institution}/approve", headers=bearer(api_token(auth_id))
    )

    assert response.status_code == 404


async def test_limited_access_may_look_and_not_act(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    await make_user(db_engine, auth_id, "viewer@example.com", role="limited_access")
    target = await make_user(db_engine, uuid4(), "applicant@example.com")
    await add_mentor(db_engine, target)

    listed = await api_client.get(f"{ADMIN}/mentors/pending", headers=bearer(api_token(auth_id)))
    acted = await api_client.post(
        f"{ADMIN}/mentors/{target}/decision", json={}, headers=bearer(api_token(auth_id))
    )

    assert listed.status_code == 200
    assert acted.status_code == 404, "limited_access decided an application"


async def test_mentor_approval_may_decide(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The positive case for the role split. Without it, refusing everyone would
    satisfy the two tests above."""
    auth_id = uuid4()
    await make_user(db_engine, auth_id, "mentors@example.com", role="mentor_approval")
    target = await make_user(db_engine, uuid4(), "applicant@example.com")
    await add_mentor(db_engine, target)

    response = await api_client.post(
        f"{ADMIN}/mentors/{target}/decision", json={}, headers=bearer(api_token(auth_id))
    )

    assert response.status_code == 200


# --------------------------------------------------------------------------
# The institution queue
# --------------------------------------------------------------------------


async def test_the_queue_ranks_by_how_many_entries_reference_each_row(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The count is the decision: many entries on one spelling is an approval,
    one entry is a typo to merge. It is computed, because `usage_count` was a
    stored counter nothing incremented."""
    auth_id = uuid4()
    await make_user(db_engine, auth_id, "admin@example.com", role="super_admin")
    # **Lonely first**, so `created_at` order is the *opposite* of the ranked
    # order. Created the other way round, the tie-break alone produces the same
    # list and a mutation dropping the ranking survives.
    await add_institution(db_engine, "Lonely College")
    popular = await add_institution(db_engine, "Popular Polytechnic")
    for index in range(3):
        user = await make_user(db_engine, uuid4(), f"student{index}@example.com")
        await add_education(db_engine, user, popular)

    body = (
        await api_client.get(f"{ADMIN}/institutions/pending", headers=bearer(api_token(auth_id)))
    ).json()

    names = [row["name"] for row in body["data"]]
    assert names.index("Popular Polytechnic") < names.index("Lonely College")
    assert body["data"][0]["uses"] == 3


async def test_an_unreferenced_row_still_appears(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The ordinary state of something typed a minute ago. A queue that hides
    new entries until a second person picks the same school is not a queue."""
    auth_id = uuid4()
    await make_user(db_engine, auth_id, "admin@example.com", role="super_admin")
    await add_institution(db_engine, "Brand New Institute")

    body = (
        await api_client.get(f"{ADMIN}/institutions/pending", headers=bearer(api_token(auth_id)))
    ).json()

    assert [row["name"] for row in body["data"]] == ["Brand New Institute"]
    assert body["data"][0]["uses"] == 0


async def test_an_approved_institution_leaves_the_queue_and_enters_search(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    await make_user(db_engine, auth_id, "admin@example.com", role="super_admin")
    institution = await add_institution(db_engine, "Approvable University")
    headers = bearer(api_token(auth_id))

    approved = await api_client.post(f"{ADMIN}/institutions/{institution}/approve", headers=headers)

    assert approved.status_code == 200
    queue = (await api_client.get(f"{ADMIN}/institutions/pending", headers=headers)).json()
    assert queue["data"] == []
    found = (await api_client.get("/api/v1/institutions", params={"q": "Approvable"})).json()
    assert [row["name"] for row in found["data"]] == ["Approvable University"]


# --------------------------------------------------------------------------
# Merging
# --------------------------------------------------------------------------


async def test_a_merge_repoints_the_entries_and_retires_the_duplicate(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Settled decision 65: the entries move, and `merged_into_id` is the audit
    trail rather than something later reads have to resolve."""
    auth_id = uuid4()
    await make_user(db_engine, auth_id, "admin@example.com", role="super_admin")
    winner = await add_institution(db_engine, "University of Lagos", status="approved")
    loser = await add_institution(db_engine, "Univerity of Lagos")
    student = await make_user(db_engine, uuid4(), "student@example.com")
    await add_education(db_engine, student, loser)

    response = await api_client.post(
        f"{ADMIN}/institutions/{loser}/merge",
        json={"winning_id": str(winner)},
        headers=bearer(api_token(auth_id)),
    )

    assert response.status_code == 200
    assert response.json()["entries_moved"] == 1
    async with db_engine.connect() as conn:
        entry = await conn.execute(text("SELECT institution_id FROM education_entries"))
        retired = await conn.execute(
            text("SELECT status, merged_into_id FROM institutions WHERE id = :i"), {"i": loser}
        )
        status_value, merged_into = retired.one()

    assert entry.scalar_one() == winner, "the entry was not repointed"
    assert str(status_value) == "merged"
    assert merged_into == winner


async def test_merging_into_a_merged_row_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Otherwise the chain grows and a read would have to follow two hops —
    which is the whole thing repointing at merge time prevents."""
    auth_id = uuid4()
    await make_user(db_engine, auth_id, "admin@example.com", role="super_admin")
    final = await add_institution(db_engine, "Real University", status="approved")
    middle = await add_institution(db_engine, "Middle University")
    first = await add_institution(db_engine, "First University")
    headers = bearer(api_token(auth_id))
    await api_client.post(
        f"{ADMIN}/institutions/{middle}/merge", json={"winning_id": str(final)}, headers=headers
    )

    response = await api_client.post(
        f"{ADMIN}/institutions/{first}/merge", json={"winning_id": str(middle)}, headers=headers
    )

    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_merging_a_row_into_itself_is_refused(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    await make_user(db_engine, auth_id, "admin@example.com", role="super_admin")
    institution = await add_institution(db_engine, "Only University")

    response = await api_client.post(
        f"{ADMIN}/institutions/{institution}/merge",
        json={"winning_id": str(institution)},
        headers=bearer(api_token(auth_id)),
    )

    assert response.status_code == 409


# --------------------------------------------------------------------------
# Mentor decisions
# --------------------------------------------------------------------------


async def test_approving_lists_the_mentor_and_records_who_decided(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Both statuses move together: approving means live in search."""
    auth_id = uuid4()
    admin = await make_user(db_engine, auth_id, "admin@example.com", role="super_admin")
    target = await make_user(db_engine, uuid4(), "applicant@example.com")
    await add_mentor(db_engine, target)

    response = await api_client.post(
        f"{ADMIN}/mentors/{target}/decision", json={}, headers=bearer(api_token(auth_id))
    )

    assert response.status_code == 200
    async with db_engine.connect() as conn:
        row = await conn.execute(
            text("SELECT approval_status, listing_status FROM mentor_profiles WHERE user_id = :u"),
            {"u": target},
        )
        approval, listing = row.one()
        # Who decided now lives in the log, not in a column on the profile.
        events = await conn.execute(
            text(
                "SELECT status_type, created_by FROM mentor_status_events "
                "WHERE mentor_user_id = :u ORDER BY status_type"
            ),
            {"u": target},
        )
        written = [(str(status), actor) for status, actor in events]

    # **Not `endswith`.** `"unlisted".endswith("listed")` is True, so the
    # obvious assertion accepts the exact value it exists to reject — a mutation
    # forcing `unlisted` on approval sailed through it.
    assert str(approval) == "approved"
    assert str(listing) == "listed"
    # Two events, not one: approval and listing are separate dimensions, and a
    # row stating both would have to copy one forward.
    assert written == [("approved", admin), ("listed", admin)]


async def test_declining_records_who_declined(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """**The column this PR added.** Reusing `approved_by` for a decline would
    put a decliner in a field named for approval, and unlike a count it cannot
    be reconstructed afterwards."""
    auth_id = uuid4()
    admin = await make_user(db_engine, auth_id, "admin@example.com", role="super_admin")
    target = await make_user(db_engine, uuid4(), "applicant@example.com")
    await add_mentor(db_engine, target)

    response = await api_client.post(
        f"{ADMIN}/mentors/{target}/decision",
        params={"approve": "false"},
        json={"reason": "not enough experience"},
        headers=bearer(api_token(auth_id)),
    )

    assert response.status_code == 200
    async with db_engine.connect() as conn:
        row = await conn.execute(
            text("SELECT approval_status, listing_status FROM mentor_profiles WHERE user_id = :u"),
            {"u": target},
        )
        approval, listing = row.one()
        events = await conn.execute(
            text(
                "SELECT status_type, reason, created_by FROM mentor_status_events "
                "WHERE mentor_user_id = :u ORDER BY status_type"
            ),
            {"u": target},
        )
        written = [(str(status), reason, actor) for status, reason, actor in events]

    assert str(approval) == "declined"
    assert str(listing) == "unlisted"
    assert written == [
        ("declined", "not enough experience", admin),
        ("unlisted", "never_approved", admin),
    ]


async def test_declining_without_a_reason_is_allowed(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Better to give one, but requiring it turns a clear-cut decision into a
    form to argue with — and an admin typing "no" to satisfy a validator has
    told nobody anything."""
    auth_id = uuid4()
    await make_user(db_engine, auth_id, "admin@example.com", role="super_admin")
    target = await make_user(db_engine, uuid4(), "applicant@example.com")
    await add_mentor(db_engine, target)

    response = await api_client.post(
        f"{ADMIN}/mentors/{target}/decision",
        params={"approve": "false"},
        json={},
        headers=bearer(api_token(auth_id)),
    )

    assert response.status_code == 200


async def test_an_admin_may_decide_their_own_application(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Permitted, and **visible** rather than prevented. On a team with one
    admin, blocking it means the only admin can never be approved at all —
    `approved_by` recording that they decided for themselves is the
    proportionate control at this size."""
    auth_id = uuid4()
    admin = await make_user(db_engine, auth_id, "admin@example.com", role="super_admin")
    await add_mentor(db_engine, admin)

    response = await api_client.post(
        f"{ADMIN}/mentors/{admin}/decision", json={}, headers=bearer(api_token(auth_id))
    )

    assert response.status_code == 200
    async with db_engine.connect() as conn:
        row = await conn.execute(
            text(
                "SELECT created_by FROM mentor_status_events "
                "WHERE mentor_user_id = :u AND status_type = 'approved'"
            ),
            {"u": admin},
        )
    assert row.scalar_one() == admin, "self-approval left no trace of who decided"


async def test_a_decided_application_leaves_the_queue(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    auth_id = uuid4()
    await make_user(db_engine, auth_id, "admin@example.com", role="super_admin")
    target = await make_user(db_engine, uuid4(), "applicant@example.com")
    await add_mentor(db_engine, target)
    headers = bearer(api_token(auth_id))

    before = (await api_client.get(f"{ADMIN}/mentors/pending", headers=headers)).json()
    await api_client.post(f"{ADMIN}/mentors/{target}/decision", json={}, headers=headers)
    after = (await api_client.get(f"{ADMIN}/mentors/pending", headers=headers)).json()

    assert len(before["data"]) == 1
    assert after["data"] == []
