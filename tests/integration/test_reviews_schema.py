"""What the ``reviews`` table guarantees, and what no gate can see.

`alembic check` reads tables, columns, types and regular indexes. Everything
here is outside that set — eight CHECK constraints, a partial unique index, two
foreign keys whose deletion behaviour is load-bearing, a server default, two
index predicates, and one measured hazard that no constraint can catch.

Deliberately **not** re-asserted here, because a second representation of one
rule is non-negotiable #8 rather than extra safety: the ``updated_at`` trigger
sweep and the CHECK-naming parity (both `test_schema_parity`), and surrogate
primary keys (ADR 0015, asserted once for every table).

Every constraint gets a rejecting **and** an accepting case. A test that only
proves a constraint refuses garbage cannot tell a working constraint from one
that refuses everything.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

LAGOS = "Africa/Lagos"
STARTS_AT = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)

#: Deliberately omits `reviewed_for_role`, so the column default is what gets
#: exercised rather than a value this file supplies.
INSERT_REVIEW = """
INSERT INTO reviews (
    session_id, reviewed_by, reviewed_for,
    communication_rating, knowledge_rating, practicality_rating, support_rating,
    valuable_rating, nps_recommend_score, public_review, private_review
)
VALUES (:session, :by, :subject, :comm, :know, :prac, :supp, :val, :nps, :public, :private)
RETURNING id
"""

INSERT_WITH_ROLE = """
INSERT INTO reviews (
    session_id, reviewed_by, reviewed_for, reviewed_for_role,
    communication_rating, knowledge_rating, practicality_rating, support_rating,
    valuable_rating, nps_recommend_score, public_review
)
VALUES (:session, :by, :subject, :role, :comm, :know, :prac, :supp, :val, :nps, :public)
RETURNING id
"""

#: The legacy scaling, written as a literal rather than bound: the point of
#: `test_a_scaled_legacy_value_rounds_instead_of_failing` is what PostgreSQL does
#: with the value the export actually carries, and a driver-side type error would
#: prove something else.
INSERT_SCALED_LEGACY = """
INSERT INTO reviews (
    reviewed_by, reviewed_for,
    communication_rating, knowledge_rating, practicality_rating, support_rating,
    valuable_rating, nps_recommend_score, public_review
)
VALUES (:by, :subject, 3.34, 1, 1, 1, 1, 1, 'legacy')
RETURNING id
"""


async def make_user(conn: AsyncConnection, email: str, role: str = "mentee") -> str:
    user_id = (
        await conn.execute(
            text(
                "INSERT INTO users (email, primary_role, timezone) "
                "VALUES (:email, :role, :tz) RETURNING id"
            ),
            {"email": email, "role": role, "tz": LAGOS},
        )
    ).scalar_one()
    return str(user_id)


async def make_mentor(conn: AsyncConnection, email: str) -> str:
    user_id = await make_user(conn, email, role="mentor")
    await conn.execute(
        text("INSERT INTO mentor_profiles (user_id, headline) VALUES (:u, 'Test mentor')"),
        {"u": user_id},
    )
    return user_id


async def insert_session(
    conn: AsyncConnection,
    mentor_id: str,
    mentee_id: str,
    starts_at: datetime = STARTS_AT,
) -> str:
    """A second session needs a second hour — `no_mentor_double_booking` is an
    exclusion constraint, so two sessions for one mentor cannot overlap."""
    result = await conn.execute(
        text(
            "INSERT INTO sessions (mentor_id, mentee_id, starts_at, duration_minutes) "
            "VALUES (:mentor, :mentee, :starts, 45) RETURNING id"
        ),
        {"mentor": mentor_id, "mentee": mentee_id, "starts": starts_at},
    )
    return str(result.scalar_one())


class Pair:
    """One mentor, one mentee, one session between them, and the connection."""

    def __init__(self, conn: AsyncConnection, mentor: str, mentee: str, session: str) -> None:
        self.conn = conn
        self.mentor = mentor
        self.mentee = mentee
        self.session = session

    async def review(
        self,
        *,
        session_id: str | None = "",
        by: str | None = None,
        subject: str | None = None,
        comm: int = 3,
        know: int = 3,
        prac: int = 2,
        supp: int = 2,
        val: int = 4,
        nps: int = 8,
        public: str | None = "He was clear, and the advice was usable.",
        private: str | None = None,
    ) -> str:
        """Insert a review, defaulting every field to a well-formed value.

        ``session_id`` defaults to the sentinel ``""`` rather than to ``None``,
        because ``None`` is a *meaningful* value here — it is the legacy shape —
        and a caller must be able to ask for it.
        """
        result = await self.conn.execute(
            text(INSERT_REVIEW),
            {
                "session": self.session if session_id == "" else session_id,
                "by": by or self.mentee,
                "subject": subject or self.mentor,
                "comm": comm,
                "know": know,
                "prac": prac,
                "supp": supp,
                "val": val,
                "nps": nps,
                "public": public,
                "private": private,
            },
        )
        return str(result.scalar_one())


@pytest_asyncio.fixture
async def pair(db_engine: AsyncEngine) -> AsyncIterator[Pair]:
    async with db_engine.begin() as conn:
        mentor = await make_mentor(conn, "mentor@example.test")
        mentee = await make_user(conn, "mentee@example.test")
        yield Pair(conn, mentor, mentee, await insert_session(conn, mentor, mentee))


async def index_definition(engine: AsyncEngine, name: str) -> str:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND indexname = :n"),
            {"n": name},
        )
        return str(result.scalar_one())


# --------------------------------------------------------------------------
# The shape of a well-formed review
# --------------------------------------------------------------------------


async def test_a_well_formed_review_is_accepted(pair: Pair) -> None:
    assert await pair.review()


async def test_a_review_is_about_the_mentor_unless_told_otherwise(pair: Pair) -> None:
    """The default is the only value anything writes today.

    `reviewed_for_role` exists because a review is *directed* — `reviewed_for`
    names a user, and a user is a mentor to one person and a mentee to another.
    The default is what makes the column free for the 53 migrated rows, which
    have no session to derive a role from.
    """
    review_id = await pair.review()

    role = await pair.conn.execute(
        text("SELECT reviewed_for_role FROM reviews WHERE id = :i"), {"i": review_id}
    )

    assert role.scalar_one() == "mentor"


async def test_the_reviewed_role_vocabulary_is_closed(pair: Pair) -> None:
    """Settled decision #100: text plus a CHECK, and the CHECK is the guard.

    The ETL writes this column with hand-written SQL and never constructs a
    model, so Pydantic is not standing between a transform bug and `'Mentor'`.
    """
    with pytest.raises(IntegrityError, match="reviewed_for_role_is_known"):
        await pair.conn.execute(
            text(INSERT_WITH_ROLE),
            {
                "session": pair.session,
                "by": pair.mentee,
                "subject": pair.mentor,
                "role": "wizard",
                "comm": 3,
                "know": 3,
                "prac": 3,
                "supp": 3,
                "val": 5,
                "nps": 10,
                "public": "Magic.",
            },
        )


# --------------------------------------------------------------------------
# The scales — finding #2 and finding #3
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", [0, 4])
async def test_a_mentor_rating_off_the_three_point_scale_is_refused(pair: Pair, value: int) -> None:
    """`1 = Not great`, `2 = Great`, `3 = Excellent`, and nothing else exists.

    The package declares these `int CHECK (BETWEEN 1 AND 5)`, which is the first
    measured contradiction that breaks the schema outright: Bubble stores the
    same three choices as `1.67 / 3.34 / 5`.
    """
    with pytest.raises(IntegrityError, match="communication_rating_range"):
        await pair.review(comm=value)


@pytest.mark.parametrize("value", [1, 2, 3])
async def test_every_point_on_the_three_point_scale_is_accepted(pair: Pair, value: int) -> None:
    assert await pair.review(comm=value, know=value, prac=value, supp=value)


@pytest.mark.parametrize("value", [0, 6])
async def test_a_valuable_rating_off_the_five_point_scale_is_refused(
    pair: Pair, value: int
) -> None:
    with pytest.raises(IntegrityError, match="valuable_rating_range"):
        await pair.review(val=value)


@pytest.mark.parametrize("value", [1, 5])
async def test_the_ends_of_the_five_point_scale_are_accepted(pair: Pair, value: int) -> None:
    assert await pair.review(val=value)


async def test_a_recommend_score_of_zero_is_refused(pair: Pair) -> None:
    """Finding #3, and the one place this table is *tighter* than the package.

    The package permits `0`. The control renders `1..10` with no zero button, so
    nothing can produce one — and a value with no producer is the defect
    decision 1 rejects `numeric` for.
    """
    with pytest.raises(IntegrityError, match="nps_recommend_score_range"):
        await pair.review(nps=0)


async def test_a_recommend_score_above_ten_is_refused(pair: Pair) -> None:
    with pytest.raises(IntegrityError, match="nps_recommend_score_range"):
        await pair.review(nps=11)


@pytest.mark.parametrize("value", [1, 10])
async def test_the_ends_of_the_recommend_scale_are_accepted(pair: Pair, value: int) -> None:
    assert await pair.review(nps=value)


async def test_a_scaled_legacy_value_rounds_instead_of_failing(pair: Pair) -> None:
    """**The CHECK cannot catch the legacy scaling, and this is the proof.**

    The handoff says the load would "fail or truncate silently". It is the
    second: `3.34` assigned to a `smallint` rounds to `3` and satisfies
    `BETWEEN 1 AND 3`, so a loader that casts instead of mapping stores
    *Excellent* where the mentee chose *Great*, with every gate green.

    The mapping `1.67 -> 1`, `3.34 -> 2`, `5 -> 3` therefore belongs in the
    transform, explicitly. This test exists so the loader's author meets that
    fact before writing the cast.
    """
    review_id = (
        await pair.conn.execute(
            text(INSERT_SCALED_LEGACY), {"by": pair.mentee, "subject": pair.mentor}
        )
    ).scalar_one()

    stored = await pair.conn.execute(
        text("SELECT communication_rating FROM reviews WHERE id = :i"), {"i": review_id}
    )

    assert stored.scalar_one() == 3, "3.34 rounded; the column is not the guard the loader needs"


# --------------------------------------------------------------------------
# The text — what the form makes compulsory
# --------------------------------------------------------------------------


async def test_a_review_without_public_text_is_refused(pair: Pair) -> None:
    """The form labels this field "Public review (required)", on step 2."""
    with pytest.raises(IntegrityError, match="public_review"):
        await pair.review(public=None)


async def test_improvement_feedback_is_optional(pair: Pair) -> None:
    """Step 3's "Improvement Feedback (optional)" — the only optional field."""
    assert await pair.review(private=None)
    assert await pair.review(
        session_id=None, private="A shared agenda beforehand would have helped."
    )


# --------------------------------------------------------------------------
# One per session, and the legacy rows that have none
# --------------------------------------------------------------------------


async def test_one_review_per_session_per_author(pair: Pair) -> None:
    await pair.review()

    with pytest.raises(IntegrityError, match="one_per_session_author"):
        await pair.review()


async def test_a_second_session_earns_a_second_review(pair: Pair) -> None:
    """Decision 6 — append, never overwrite.

    The legacy app updated one row per pair. A review is about a *session*, so
    editing session A's row because the mentee attended session B leaves a row
    claiming to be about A while describing B.
    """
    await pair.review()
    later = await insert_session(pair.conn, pair.mentor, pair.mentee, STARTS_AT + timedelta(days=7))

    assert await pair.review(session_id=later)


async def test_the_migrated_rows_are_not_collapsed_by_the_unique_index(pair: Pair) -> None:
    """Decision 3 and decision 5, asserted together.

    The 53 legacy reviews carry no session — the legacy `Reviews` type has no
    link to one, confirmed three ways — so `session_id` is nullable and the
    uniqueness rule is partial on `session_id IS NOT NULL`. A plain UNIQUE would
    rely on PostgreSQL treating NULLs as distinct, which is the same behaviour
    resting on an unstated assumption rather than on a written predicate.
    """
    assert await pair.review(session_id=None)
    assert await pair.review(session_id=None)


async def test_a_review_of_oneself_is_refused(pair: Pair) -> None:
    with pytest.raises(IntegrityError, match="no_self_review"):
        await pair.review(by=pair.mentor, subject=pair.mentor)


# --------------------------------------------------------------------------
# A review is evidence — ADR 0013's RESTRICT half
# --------------------------------------------------------------------------


async def test_a_reviewed_session_cannot_be_deleted(pair: Pair) -> None:
    """RESTRICT, not CASCADE: the review records a fact about a real hour.

    ADR 0013's rule is cascade where the child is meaningless without its parent
    and records no auditable fact, restrict where it is evidence. A review is
    evidence, and it is also *published* — a mentor's profile shows it.
    """
    await pair.review()

    with pytest.raises(IntegrityError, match="fk_reviews_session_id_sessions"):
        await pair.conn.execute(text("DELETE FROM sessions WHERE id = :i"), {"i": pair.session})


async def test_the_author_of_a_review_cannot_be_deleted(pair: Pair) -> None:
    """A migrated author, whose only reference is the review itself.

    Deleting `pair.mentee` would prove nothing: `fk_sessions_mentee_id_users`
    refuses first, and a test that passes on somebody else's constraint is a
    test of nothing.
    """
    author = await make_user(pair.conn, "migrated@example.test")
    await pair.review(session_id=None, by=author)

    with pytest.raises(IntegrityError, match="fk_reviews_reviewed_by_users"):
        await pair.conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": author})


# --------------------------------------------------------------------------
# Soft delete, and the index that respects it
# --------------------------------------------------------------------------


async def test_a_withdrawn_review_keeps_its_row(pair: Pair) -> None:
    """Soft delete, which the canonical DDL carries and `sessions` deliberately
    does not.

    Public review text is the one thing on this platform that will need
    moderation, and a hard delete of evidence contradicts decision 6's ledger
    argument as directly as an overwrite would.
    """
    review_id = await pair.review()

    await pair.conn.execute(
        text("UPDATE reviews SET deleted_at = now() WHERE id = :i"), {"i": review_id}
    )
    still_there = await pair.conn.execute(
        text("SELECT count(*) FROM reviews WHERE id = :i"), {"i": review_id}
    )

    assert still_there.scalar_one() == 1


async def test_the_card_index_covers_live_mentor_reviews_only(db_engine: AsyncEngine) -> None:
    """The index PR 3's discovery card reads, shipped ahead of its reader.

    `ix_sessions_mentor_completed` is the precedent: a partial index that existed
    from the M4 schema and had no reader until the card arrived. Both halves of
    the predicate are load-bearing — a withdrawn review must not move a mentor's
    average, and a mentee-directed review must not enter a mentor's at all.
    """
    definition = await index_definition(db_engine, "ix_reviews_mentor_valuable")

    assert "reviewed_for" in definition
    assert "valuable_rating" in definition
    assert "deleted_at IS NULL" in definition
    assert "reviewed_for_role = 'mentor'" in definition


async def test_the_eligibility_index_orders_by_recency(db_engine: AsyncEngine) -> None:
    """The interval window's index — decision 7's predicate, served.

    *Has this mentee reviewed this offering recently* reaches the offering by
    joining `sessions`, so what this table is asked for is the author and the
    date. `reviewed_for` was here for a mentor-scoped window that no longer
    exists, and it left in `c9e4b1f78d02`.

    **It carries no `deleted_at` predicate, and that is decision 18 rather than
    an omission.** Withdrawing a review removes it from what is *published*, not
    from what *happened* — so a withdrawn review still suppresses the window.
    The alternative lets a mentee whose abusive review was moderated away submit
    a replacement immediately, which is the incentive exactly inverted.

    The cost was measured rather than assumed: adding `AND deleted_at IS NULL`
    to the query drops it from an Index Only Scan with zero heap fetches to an
    Index Scan with a heap filter. Asserting the *absence* of the predicate is
    what stops somebody adding it for symmetry with the card index, whose
    exclusion of withdrawn rows is the deliberate opposite.
    """
    definition = await index_definition(db_engine, "ix_reviews_author_created")

    assert "reviewed_by" in definition
    assert "created_at DESC" in definition
    assert "reviewed_for" not in definition, (
        "the interval is scoped to the offering, reached by joining `sessions`, so "
        "nothing filters `reviewed_for` here — a third column costs 31% index size "
        "and every insert maintains it for no reader"
    )
    assert "deleted_at" not in definition, (
        "a withdrawn review still suppresses the interval (decision 18); "
        "excluding it here would let moderation hand back a fresh review slot"
    )


# --------------------------------------------------------------------------
# The join back to Bubble
# --------------------------------------------------------------------------


async def test_a_legacy_id_cannot_be_loaded_twice(pair: Pair) -> None:
    """What makes the migration re-runnable, and decision 1 reversible.

    The scale mapping is lossless *because* `legacy_bubble_id` keeps the join
    back to the row that carried `3.34`.
    """
    legacy = f"{uuid.uuid4().hex}x{uuid.uuid4().hex}"
    statement = text(
        "INSERT INTO reviews (reviewed_by, reviewed_for, communication_rating, "
        "knowledge_rating, practicality_rating, support_rating, valuable_rating, "
        "nps_recommend_score, public_review, legacy_bubble_id) "
        "VALUES (:by, :subject, 1, 1, 1, 1, 1, 1, 'migrated', :legacy)"
    )
    values = {"by": pair.mentee, "subject": pair.mentor, "legacy": legacy}

    await pair.conn.execute(statement, values)

    with pytest.raises(IntegrityError, match="legacy_bubble_id"):
        await pair.conn.execute(statement, values)
