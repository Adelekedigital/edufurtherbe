"""What a mentor offers, as **they** see it — including what they have switched off.

**The endpoint exists because the public one structurally cannot serve this
screen.** `GET /users/{id}/session-types` takes no token and filters through
`session_type_is_live()` + `mentor_is_public()`, so it returns only *active*
offerings belonging to an *approved and listed* mentor. A mentor managing their
own list has to see the paused ones, and has to see them while their profile is
unlisted or still pending review. Every test below is a statement about one of
those differences.

**Two mentors, always, wherever a scope is the thing under test.** A fixture
holding one of something cannot test a filter on it — with a single mentor in the
database, "this mentor's rows" and "all rows" are the same set, and
`SessionType.mentor_user_id == user_id` could be deleted with every assertion
still green. That has now cost this repository three pull requests in a row.

**And the caller is always a mentor who legitimately has rows.** A refusal test
proves whichever layer refuses first, and the outermost layer always wins: if the
caller were a stranger, `get_current_user` would answer before the store's `WHERE`
was reached and the scope would go untested. Here there is no outer layer to hide
behind — `CurrentUserDep` resolves any authenticated user, so the query's own
predicate is the only thing standing between mentor A and mentor B's offerings.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from tests.integration.factories import add_session_type, make_public_mentor

from app.infra.db.session_type_store import _own_session_types, list_own_session_types
from conftest import add_user, api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.anyio]

URL = "/api/v1/me/session-types"


def public_url(mentor: UUID) -> str:
    return f"/api/v1/users/{mentor}/session-types"


async def token_for(engine: AsyncEngine, user: UUID) -> str:
    """A bearer token authenticating as ``user``.

    `users.auth_id` is what `get_current_user` matches the token's subject
    against. `make_public_mentor` generates one and does not hand it back, and
    `add_user` leaves it null — the state every migrated user starts in and one
    no token can match. So this writes a known value rather than reading either
    back, and one helper covers both.

    Local to this file deliberately: one other suite does the same `UPDATE`
    inline, and two callers is not the threshold for a shared factory.
    """
    auth_id = uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET auth_id = :a WHERE id = :u"), {"a": auth_id, "u": user}
        )
    return api_token(auth_id)


async def as_mentor(engine: AsyncEngine, tag: str, **knobs: object) -> tuple[UUID, str]:
    """A public mentor and a token that authenticates as them."""
    mentor = await make_public_mentor(engine, tag, **knobs)  # type: ignore[arg-type]
    return mentor, await token_for(engine, mentor)


async def names(client: httpx.AsyncClient, token: str) -> list[str]:
    response = await client.get(URL, headers=bearer(token))
    assert response.status_code == 200, response.text
    return [offering["name"] for offering in response.json()["data"]]


# --------------------------------------------------------------------------
# The three things the public endpoint cannot say
# --------------------------------------------------------------------------


async def test_a_mentor_sees_their_own_switched_off_offering(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The case this endpoint exists for, asserted against both endpoints at once.

    Asserting only that the owner sees two would pass against an implementation
    that had quietly kept `is_active` in the predicate and happened to be handed
    two live rows. The contrast is the assertion: the same database, the same
    mentor, one offering visible publicly and two visible to its owner.
    """
    mentor, token = await as_mentor(db_engine, "own-paused")
    await add_session_type(db_engine, mentor, name="Live one")
    await add_session_type(db_engine, mentor, name="Paused one", active=False)

    owner = await api_client.get(URL, headers=bearer(token))
    public = await api_client.get(public_url(mentor))

    assert owner.status_code == 200
    assert [o["name"] for o in owner.json()["data"]] == ["Live one", "Paused one"]
    assert [o["is_active"] for o in owner.json()["data"]] == [True, False]
    assert [o["name"] for o in public.json()["data"]] == ["Live one"], (
        "the public endpoint published a switched-off offering"
    )


@pytest.mark.parametrize(
    ("tag", "knob"),
    [("unlisted", {"listed": False}), ("pending", {"approved": False})],
)
async def test_a_mentor_sees_their_own_offerings_while_not_publicly_visible(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine, tag: str, knob: dict[str, bool]
) -> None:
    """A paused mentor still manages their offerings.

    Both halves separately, because `apply_mentor_status` writes approval or
    listing and never both — `pending` + `listed` is a legal row, so a predicate
    checking one would pass the test for the other.

    The public endpoint answers 404 for both of these. That is correct there and
    would be useless here: a mentor who has been unlisted pending review is
    exactly the mentor most likely to be looking at this screen.
    """
    mentor, token = await as_mentor(db_engine, f"invisible-{tag}", **knob)
    await add_session_type(db_engine, mentor, name="Still mine")

    assert (await api_client.get(public_url(mentor))).status_code == 404
    assert await names(api_client, token) == ["Still mine"]


async def test_the_list_holds_only_the_callers_offerings(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The owner scope, against a database that contains somebody else.

    Both mentors are public, both hold offerings, and the caller is a legitimate
    mentor reading their own list — so nothing refuses before the store, and
    `mentor_user_id == user_id` in the `WHERE` is the only reason mentor B's row
    is absent. Deleting it turns this into a 200 full of other mentors' products.
    """
    mine, token = await as_mentor(db_engine, "scope-mine")
    theirs = await make_public_mentor(db_engine, "scope-theirs")
    await add_session_type(db_engine, mine, name="Mine")
    await add_session_type(db_engine, theirs, name="Theirs")

    assert await names(api_client, token) == ["Mine"]


# --------------------------------------------------------------------------
# What stays hidden, and from whom
# --------------------------------------------------------------------------


async def test_a_soft_deleted_offering_is_absent(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """`is_active` is reversible and `deleted_at` is not, which is why the owner
    view drops one predicate and keeps the other."""
    mentor, token = await as_mentor(db_engine, "own-deleted")
    await add_session_type(db_engine, mentor, name="Kept")
    await add_session_type(db_engine, mentor, name="Removed", deleted=True)

    assert await names(api_client, token) == ["Kept"]


async def test_an_offering_with_no_booking_config_is_absent(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Pinned because it is join *shape* rather than a predicate anybody wrote.

    An inner join, matching the public endpoint: duration, notice and venue all
    come from the config, and there is no row to read them from. Nothing in
    `api/` can currently create an offering without one, so this is not a state
    the product reaches — the factory is the only thing that can build it.

    It is asserted rather than left implicit because the owner-facing argument
    for a `LEFT JOIN` is real and will be made again when the write endpoints
    land: a mentor arguably should see an offering that needs finishing. That is
    a contract change — three required response fields would become nullable —
    so it belongs in the pull request that can create such a row, not this one.
    """
    mentor, token = await as_mentor(db_engine, "own-no-config")
    await add_session_type(db_engine, mentor, name="Configured")
    await add_session_type(db_engine, mentor, name="Unconfigured", config=False)

    assert await names(api_client, token) == ["Configured"]


async def test_a_user_who_is_not_a_mentor_gets_an_empty_page(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """200 with nothing in it, not a 403 and not a 404.

    `session_types.mentor_user_id` references `mentor_profiles`, so a user with no
    mentor profile cannot own a row and the honest answer is an empty list — the
    same reasoning the public endpoint already applies to a visible mentor who
    offers nothing. A 403 would also be a claim about what this account *is*,
    which the caller did not ask about.

    Another mentor holds an offering, so an empty result is a scoping answer
    rather than an empty table.
    """
    other = await make_public_mentor(db_engine, "not-me")
    await add_session_type(db_engine, other, name="Not yours")

    async with db_engine.begin() as conn:
        mentee = await add_user(conn, "mentee-only@example.test")

    response = await api_client.get(URL, headers=bearer(await token_for(db_engine, mentee)))

    assert response.status_code == 200
    assert response.json()["data"] == []


async def test_a_request_with_no_token_is_refused(api_client: httpx.AsyncClient) -> None:
    """The whole difference from the endpoint beside it. `/users/{id}/session-types`
    answers a caller holding nothing; this one must not."""
    assert (await api_client.get(URL)).status_code == 401


# --------------------------------------------------------------------------
# The order, and the constraint the order rests on
# --------------------------------------------------------------------------


async def test_offerings_are_ordered_by_name(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """Seeded so insertion order is not alphabetical, or the sort proves nothing.

    A switched-off offering is in the set deliberately: this endpoint orders a
    **wider** row set than the public one, and the ordering has to be total
    across all of it rather than only across the live rows.
    """
    mentor, token = await as_mentor(db_engine, "own-ordered")
    await add_session_type(db_engine, mentor, name="Visa questions")
    await add_session_type(db_engine, mentor, name="Application review", active=False)
    await add_session_type(db_engine, mentor, name="Mock interview")

    assert await names(api_client, token) == [
        "Application review",
        "Mock interview",
        "Visa questions",
    ]


async def test_two_undeleted_offerings_cannot_share_a_name(db_engine: AsyncEngine) -> None:
    """The premise the ordering rests on, asserted rather than assumed.

    Ordering by `name` is *total* rather than merely usually-stable only because
    a mentor cannot hold two rows with the same one. The index is
    `UNIQUE (mentor_user_id, name) WHERE deleted_at IS NULL` — note the
    predicate is soft-deletion, **not** `is_active`, which is what makes the
    guarantee survive this endpoint's wider row set. A switched-off offering is
    still undeleted and still covered, which is why the first row here is one.

    This passes on the first run, and that is correct: it pins a constraint that
    already exists, against a later change that would silently make this
    endpoint's ordering non-deterministic.
    """
    mentor = await make_public_mentor(db_engine, "own-unique")
    await add_session_type(db_engine, mentor, name="Duplicate", active=False)

    with pytest.raises(IntegrityError, match="mentor_user_id"):
        await add_session_type(db_engine, mentor, name="Duplicate")


async def test_a_soft_deleted_name_can_be_taken_again(db_engine: AsyncEngine) -> None:
    """The other half of that predicate, and the reason it is partial.

    Without `WHERE deleted_at IS NULL` a mentor could never reuse the name of an
    offering they had removed. Asserting only the refusal above would pass
    against a plain unique index, which is a different and worse constraint.
    """
    mentor = await make_public_mentor(db_engine, "own-reuse")
    await add_session_type(db_engine, mentor, name="Recycled", deleted=True)

    await add_session_type(db_engine, mentor, name="Recycled")


# --------------------------------------------------------------------------
# The contract, and the one it must not change
# --------------------------------------------------------------------------


async def test_the_response_carries_the_three_owner_only_fields(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """An allowlist, asserted as one — and the three additions asserted by value.

    Asserting the key set alone would pass against an endpoint returning
    `category: null` for a row that has one, which is the failure a mentor would
    actually see.
    """
    mentor, token = await as_mentor(db_engine, "own-fields")
    await add_session_type(
        db_engine,
        mentor,
        name="Full",
        description="Everything filled in",
        category="internal-classification",
        application_stage="postgrad",
        active=False,
    )

    (offering,) = (await api_client.get(URL, headers=bearer(token))).json()["data"]

    assert set(offering) == {
        "id",
        "name",
        "description",
        "duration_minutes",
        "min_notice_minutes",
        "meeting_venue",
        "is_active",
        "category",
        "application_stage",
    }
    assert offering["is_active"] is False
    assert offering["category"] == "internal-classification"
    assert offering["application_stage"] == "postgrad"


async def test_the_public_contract_did_not_gain_the_owner_only_fields(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    """The Definition of Done's one negative claim, asserted here rather than trusted.

    `SessionTypeRead` and `OwnSessionTypeRead` are separate models precisely so
    the owner view can grow without the public one following. Nothing in the gate
    compares two response schemas, so this is what keeps them apart — and it is
    what would fail if the two were ever given a shared base class that quietly
    leaked a field downward.
    """
    mentor = await make_public_mentor(db_engine, "public-unchanged")
    await add_session_type(
        db_engine, mentor, name="Public", category="internal", application_stage="postgrad"
    )

    response = await api_client.get(public_url(mentor))
    (offering,) = response.json()["data"]

    assert set(offering) == {
        "id",
        "name",
        "description",
        "duration_minutes",
        "min_notice_minutes",
        "meeting_venue",
    }
    assert "internal" not in response.text
    assert "postgrad" not in response.text


def test_a_static_meeting_room_has_nowhere_left_to_leak_from() -> None:
    """`custom_meeting_url` is gone, so the leak it guarded is structural now.

    This was a live test on this branch: it seeded a mentor with a custom room
    and asserted the URL never reached the response. **D88's contract step
    deleted the column** — it was never moved onto the offering, because nothing
    read it — so the fixture can no longer create the case, and no query can
    return what no table stores.

    Kept as a schema assertion rather than deleted outright. The original guarded
    a bearer credential, and a column reintroduced later under the same name
    would restore the exposure with the behavioural test long since removed. This
    fails the moment one comes back, and points at the decision it has to clear.
    """
    from app.infra.db.models.mentoring import MentorProfile
    from app.infra.db.models.sessions import SessionTypeBookingConfig

    for model in (MentorProfile, SessionTypeBookingConfig):
        assert "custom_meeting_url" not in model.__table__.columns, (
            f"{model.__tablename__} has custom_meeting_url again — it is a bearer "
            "credential, and the endpoint tests that kept it out of responses were "
            "removed when D88's contract step dropped it"
        )


# --------------------------------------------------------------------------
# The plan, and the ordering the plan was hiding
# --------------------------------------------------------------------------


async def test_the_order_comes_from_the_query_and_not_from_the_index(
    db_engine: AsyncEngine,
) -> None:
    """Deleting `.order_by(SessionType.name)` left the whole suite green.

    Not because the clause is redundant — because the partial unique index on
    `(mentor_user_id, name) WHERE deleted_at IS NULL` covers this query's `WHERE`
    exactly, so an index scan hands the rows back in name order for free. The
    endpoint was sorted by accident, and every assertion above agreed with it.

    That is a property of the *plan*, not of the query, and a plan is chosen from
    statistics and row counts. At a few hundred offerings the planner switches to
    a sequential scan and the free ordering disappears — silently, in production,
    on a screen whose rows would simply start moving between refreshes.

    So the index scan is turned off and the same store function is called again.
    Now only the `ORDER BY` can produce the order, and removing it fails here.
    """
    mentor = await make_public_mentor(db_engine, "own-order-unindexed")
    for name in ("Visa questions", "Application review", "Mock interview"):
        await add_session_type(db_engine, mentor, name=name)

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as session:
        await session.execute(text("SET enable_indexscan = off"))
        await session.execute(text("SET enable_bitmapscan = off"))
        rows = await list_own_session_types(session, mentor)

    assert [row["name"] for row in rows] == [
        "Application review",
        "Mock interview",
        "Visa questions",
    ], "the rows came back in heap order — the ORDER BY is not doing the work"


async def test_the_owner_read_can_use_the_mentor_index(db_engine: AsyncEngine) -> None:
    """The guardrail's `EXPLAIN`, on the real statement rather than a retyped one.

    The plan is read off the query the store actually builds, compiled with its
    literals — a hand-written copy in the test would be a second representation
    of the thing under test, and the copy that drifts is the one that keeps
    passing.

    `enable_seqscan = off` because at test scale every plan is a sequential scan
    and the question is whether an index *exists and is usable*, not what the
    planner picks over a dozen rows.
    """
    mentor = await make_public_mentor(db_engine, "own-plan")
    await add_session_type(db_engine, mentor, name="Planned")

    statement = _own_session_types(mentor).compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    )

    async with db_engine.connect() as conn:
        await conn.execute(text("SET enable_seqscan = off"))
        plan = "\n".join(
            str(line) for line in (await conn.execute(text(f"EXPLAIN {statement}"))).scalars()
        )

    # Named exactly. The first draft of this assertion allowed `or "Index" in
    # plan`, which is true of almost any plan and made the test unfailable — and
    # it passed while naming an index that does not exist.
    assert "ix_session_types_mentor_name" in plan, plan
    assert "Seq Scan on session_types" not in plan, plan
