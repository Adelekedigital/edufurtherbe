"""Constraints on the M2 profile tables that no model can express.

``alembic check`` sees tables, columns, types and regular indexes. It is blind to
partial indexes, ``CHECK`` constraints and which column a foreign key points at
— which here is most of what makes the schema correct.

The first two tests matter more than the rest. The package keys mentor-only and
mentee-only tables on ``mentor_profiles(user_id)`` and ``mentee_goals(user_id)``
rather than on ``users(id)``, and the values are identical, so **repointing them
at ``users`` is a one-word edit that compiles, migrates and passes every other
test in this file** while silently deleting the only structural protection those
tables have.
"""

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from conftest import add_user

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

THIS_YEAR = datetime.now(UTC).year


# A table and a column cannot be bound parameters, so this helper interpolates
# them — and the pair is checked against this frozen set first. That is what
# makes the suppression below verifiable rather than a promise: `value` is bound,
# and the only interpolated text is one of these six literals.
LOOKUPS: frozenset[tuple[str, str]] = frozenset(
    {
        ("service_offerings", "slug"),
        ("countries", "code"),
        ("scholarship_programs", "slug"),
    }
)


async def lookup_id(conn: AsyncConnection, table: str, column: str, value: str) -> uuid.UUID:
    """Resolve a seeded row's natural key to the id this environment generated.

    Never a literal id: reference ids are generated per database (ADR 0015), so
    a hard-coded one would be correct here and silently wrong everywhere else.
    """
    if (table, column) not in LOOKUPS:
        raise AssertionError(f"unknown lookup {table}.{column}")
    result = await conn.execute(
        text(f"SELECT id FROM {table} WHERE {column} = :v"),  # noqa: S608 — see LOOKUPS
        {"v": value},
    )
    return result.scalar_one()


async def add_mentor_profile(
    conn: AsyncConnection,
    user_id: uuid.UUID,
) -> uuid.UUID:
    result = await conn.execute(
        text("INSERT INTO mentor_profiles (user_id) VALUES (:u) RETURNING id"),
        {"u": user_id},
    )
    return result.scalar_one()


async def add_mentee_goals(conn: AsyncConnection, user_id: uuid.UUID) -> uuid.UUID:
    result = await conn.execute(
        text("INSERT INTO mentee_goals (user_id) VALUES (:u) RETURNING id"), {"u": user_id}
    )
    return result.scalar_one()


async def add_education(
    conn: AsyncConnection,
    user_id: uuid.UUID,
    *,
    school: str = "University of Lagos",
    is_most_recent: bool = False,
    date_start: date | None = None,
    date_end: date | None = None,
    deleted_at: datetime | None = None,
) -> uuid.UUID:
    result = await conn.execute(
        text(
            "INSERT INTO education_entries "
            "(user_id, school_name_raw, is_most_recent, date_start, date_end, deleted_at) "
            "VALUES (:u, :s, :m, :ds, :de, :d) RETURNING id"
        ),
        {
            "u": user_id,
            "s": school,
            "m": is_most_recent,
            "ds": date_start,
            "de": date_end,
            "d": deleted_at,
        },
    )
    return result.scalar_one()


async def add_award(
    conn: AsyncConnection,
    user_id: uuid.UUID,
    *,
    title: str = "Chevening Award",
    year: int | None = None,
    programme: uuid.UUID | None = None,
) -> uuid.UUID:
    result = await conn.execute(
        text(
            "INSERT INTO user_awards (user_id, institution, title, year, scholarship_program_id) "
            "VALUES (:u, 'Somewhere', :t, :y, :p) RETURNING id"
        ),
        {"u": user_id, "t": title, "y": year, "p": programme},
    )
    return result.scalar_one()


# --------------------------------------------------------------------------
# The foreign-key targets that carry the mentor/mentee guarantee
# --------------------------------------------------------------------------


async def test_a_mentee_cannot_be_given_a_mentor_service_offering(db_engine: AsyncEngine) -> None:
    """A user with no mentor profile cannot offer a service.

    This is the whole reason ``mentor_user_id`` references
    ``mentor_profiles(user_id)`` instead of ``users(id)``. The values are the
    same; the guarantee is not.
    """
    async with db_engine.begin() as conn:
        mentee = await add_user(conn, "mentee@example.com")
        offering = await lookup_id(conn, "service_offerings", "slug", "test-preparation")

        with pytest.raises(IntegrityError):
            await conn.execute(
                text(
                    "INSERT INTO mentor_service_offerings (mentor_user_id, service_offering_id) "
                    "VALUES (:u, :s)"
                ),
                {"u": mentee, "s": offering},
            )


async def test_a_mentor_can_be_given_a_service_offering(db_engine: AsyncEngine) -> None:
    """The positive case. Without it the test above passes on a broken schema."""
    async with db_engine.begin() as conn:
        mentor = await add_user(conn, "mentor@example.com")
        await add_mentor_profile(conn, mentor)
        offering = await lookup_id(conn, "service_offerings", "slug", "test-preparation")

        await conn.execute(
            text(
                "INSERT INTO mentor_service_offerings (mentor_user_id, service_offering_id) "
                "VALUES (:u, :s)"
            ),
            {"u": mentor, "s": offering},
        )
        count = await conn.execute(text("SELECT count(*) FROM mentor_service_offerings"))

    assert count.scalar_one() == 1


async def test_a_mentor_cannot_list_the_same_offering_twice(db_engine: AsyncEngine) -> None:
    """The unique pair that the package's composite primary key used to carry."""
    async with db_engine.begin() as conn:
        mentor = await add_user(conn, "mentor@example.com")
        await add_mentor_profile(conn, mentor)
        offering = await lookup_id(conn, "service_offerings", "slug", "document-preparation")
        insert = text(
            "INSERT INTO mentor_service_offerings (mentor_user_id, service_offering_id) "
            "VALUES (:u, :s)"
        )
        await conn.execute(insert, {"u": mentor, "s": offering})

        with pytest.raises(IntegrityError):
            await conn.execute(insert, {"u": mentor, "s": offering})


@pytest.mark.parametrize(
    ("table", "second_column", "lookup"),
    [
        ("mentee_goal_countries", "country_id", ("countries", "code", "NG")),
        (
            "mentee_goal_needs",
            "service_offering_id",
            ("service_offerings", "slug", "school-selection"),
        ),
    ],
)
async def test_a_goal_row_requires_a_goals_record(
    db_engine: AsyncEngine,
    table: str,
    second_column: str,
    lookup: tuple[str, str, str],
) -> None:
    """Both goal junctions reference ``mentee_goals(user_id)``, not ``users(id)``.

    A target country or a stated need belongs to a goal. A user who has never
    set goals cannot have either, and the database is what says so.
    """
    async with db_engine.begin() as conn:
        user = await add_user(conn, "nogoals@example.com")
        other = await lookup_id(conn, *lookup)

        with pytest.raises(IntegrityError):
            await conn.execute(
                text(
                    f"INSERT INTO {table} (user_id, {second_column}) VALUES (:u, :o)"  # noqa: S608
                ),
                {"u": user, "o": other},
            )


async def test_a_goal_row_is_accepted_once_goals_exist(db_engine: AsyncEngine) -> None:
    """The positive half of the pair above."""
    async with db_engine.begin() as conn:
        user = await add_user(conn, "hasgoals@example.com")
        await add_mentee_goals(conn, user)
        nigeria = await lookup_id(conn, "countries", "code", "NG")

        await conn.execute(
            text("INSERT INTO mentee_goal_countries (user_id, country_id) VALUES (:u, :c)"),
            {"u": user, "c": nigeria},
        )
        count = await conn.execute(text("SELECT count(*) FROM mentee_goal_countries"))

    assert count.scalar_one() == 1


# --------------------------------------------------------------------------
# education_entries — the one-most-recent partial unique index
# --------------------------------------------------------------------------


async def test_a_user_may_have_only_one_most_recent_entry(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        user = await add_user(conn, "grad@example.com")
        await add_education(conn, user, school="University of Lagos", is_most_recent=True)

        with pytest.raises(IntegrityError):
            await add_education(conn, user, school="University of Oxford", is_most_recent=True)


async def test_many_entries_may_be_not_most_recent(db_engine: AsyncEngine) -> None:
    """The `WHERE` clause is the whole constraint.

    Without it this is a plain unique index on ``user_id`` and a user may hold
    exactly one degree — which no test that inserts a single row would catch.
    """
    async with db_engine.begin() as conn:
        user = await add_user(conn, "grad@example.com")
        for school in ("Unilag", "Oxford", "Toronto"):
            await add_education(conn, user, school=school, is_most_recent=False)

        count = await conn.execute(text("SELECT count(*) FROM education_entries"))

    assert count.scalar_one() == 3


async def test_a_soft_deleted_entry_frees_the_most_recent_slot(db_engine: AsyncEngine) -> None:
    """``deleted_at IS NULL`` is the other half of the predicate.

    Without it a soft-deleted degree permanently blocks the user from marking a
    newer one as most recent, and the failure reads as "you already have one".
    """
    async with db_engine.begin() as conn:
        user = await add_user(conn, "grad@example.com")
        await add_education(
            conn, user, school="Unilag", is_most_recent=True, deleted_at=datetime.now(UTC)
        )
        await add_education(conn, user, school="Oxford", is_most_recent=True)

        live = await conn.execute(
            text("SELECT count(*) FROM education_entries WHERE deleted_at IS NULL")
        )

    assert live.scalar_one() == 1


async def test_an_entry_ending_before_it_starts_is_rejected(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        user = await add_user(conn, "grad@example.com")

        with pytest.raises(IntegrityError):
            await add_education(conn, user, date_start=date(2024, 1, 1), date_end=date(2023, 1, 1))


async def test_an_entry_starting_and_ending_the_same_day_is_accepted(
    db_engine: AsyncEngine,
) -> None:
    """The boundary. ``>=``, not ``>`` — a one-day course is legal."""
    async with db_engine.begin() as conn:
        user = await add_user(conn, "grad@example.com")
        entry = await add_education(
            conn, user, date_start=date(2024, 1, 1), date_end=date(2024, 1, 1)
        )

    assert entry is not None


# --------------------------------------------------------------------------
# user_awards
# --------------------------------------------------------------------------


async def test_an_award_dated_next_year_is_accepted(db_engine: AsyncEngine) -> None:
    """Both sides of the boundary are asserted, because only one of them moves."""
    async with db_engine.begin() as conn:
        user = await add_user(conn, "scholar@example.com")
        award = await add_award(conn, user, year=THIS_YEAR + 1)

    assert award is not None


async def test_an_award_dated_two_years_out_is_rejected(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        user = await add_user(conn, "scholar@example.com")

        with pytest.raises(IntegrityError):
            await add_award(conn, user, year=THIS_YEAR + 2)


async def test_an_award_may_link_to_a_seeded_programme(db_engine: AsyncEngine) -> None:
    """The column that gives ``scholarship_programs`` its only consumer."""
    async with db_engine.begin() as conn:
        user = await add_user(conn, "scholar@example.com")
        chevening = await lookup_id(conn, "scholarship_programs", "slug", "chevening")
        await add_award(conn, user, title="Chevening", programme=chevening)

        linked = await conn.execute(
            text(
                "SELECT p.display_name FROM user_awards a "
                "JOIN scholarship_programs p ON p.id = a.scholarship_program_id"
            )
        )

    assert linked.scalar_one() == "Chevening UK Government Scholarship"


async def test_an_unlinked_award_still_records_its_title(db_engine: AsyncEngine) -> None:
    """``title`` is always kept and the link is optional — the same shape as
    ``school_name_raw`` + ``institution_id``.

    This is what makes an incomplete catalogue a filtering concern rather than
    data loss: the award renders whatever the user typed either way.
    """
    async with db_engine.begin() as conn:
        user = await add_user(conn, "scholar@example.com")
        await add_award(conn, user, title="Best Graduating Student")

        row = await conn.execute(text("SELECT title, scholarship_program_id FROM user_awards"))
        title, programme = row.one()

    assert title == "Best Graduating Student"
    assert programme is None


# --------------------------------------------------------------------------
# mentor_profiles
# --------------------------------------------------------------------------


# `test_a_custom_meeting_url_requires_the_custom_venue` and
# `test_a_custom_url_is_accepted_with_the_custom_venue` were deleted here by
# D88's contract step, along with the constraint they pinned.
#
# They asserted that a static personal room could not be stored against a Meet or
# Daily profile — a privacy incident, because both create a per-session link and a
# stored URL would be shared by back-to-back sessions. `custom_meeting_url` was
# removed rather than moved: nothing in `src/` had ever written it.
#
# **`MeetingProvider.CUSTOM` still exists and now has nowhere to keep a URL.** If
# booking gives it one, these two tests are the shape the new constraint needs,
# and this comment is here so they are rewritten rather than reinvented.


async def test_a_user_may_hold_only_one_mentor_profile(db_engine: AsyncEngine) -> None:
    """The UNIQUE that replaced the package's primary key.

    Drop it and two profiles per mentor become legal, silently, with nothing
    surfacing it until somebody reads the wrong one.
    """
    async with db_engine.begin() as conn:
        mentor = await add_user(conn, "mentor@example.com")
        await add_mentor_profile(conn, mentor)

        with pytest.raises(IntegrityError):
            await add_mentor_profile(conn, mentor)


async def test_a_user_may_hold_only_one_goals_row(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        user = await add_user(conn, "mentee@example.com")
        await add_mentee_goals(conn, user)

        with pytest.raises(IntegrityError):
            await add_mentee_goals(conn, user)


# `test_booking_confirmation_defaults_to_false` was deleted by D88's contract
# step. It pinned `mentor_profiles.requires_booking_confirmation` defaulting to
# `false` — a departure from the package, asserted so that changing it back would
# be deliberate.
#
# The departure survives; the column does not. The same default now sits on
# `session_type_booking_configs.requires_booking_confirmation`, which is where
# every reader and the fan-out writer look, and the reasoning is unchanged:
# legacy stored a blank on 10 of 15 mentors and blank meant "never turned it on".


async def test_a_new_profile_is_pending_and_unlisted(db_engine: AsyncEngine) -> None:
    """Approval and listing are separate, and both start closed."""
    async with db_engine.begin() as conn:
        mentor = await add_user(conn, "mentor@example.com")
        await add_mentor_profile(conn, mentor)

        row = await conn.execute(
            text("SELECT approval_status::text, listing_status::text FROM mentor_profiles")
        )
        approval, listing = row.one()
        # A brand-new profile has no history: nothing has decided anything yet.
        events = await conn.execute(text("SELECT count(*) FROM mentor_status_events"))

    assert approval == "pending"
    assert listing == "unlisted"
    assert events.scalar_one() == 0
    # Right for a new signup, wrong for every migrated mentor — the M2c transform
    # sets this explicitly rather than inheriting it.


# --------------------------------------------------------------------------
# The full-text index deferred from M1
# --------------------------------------------------------------------------


async def test_the_about_me_index_is_used_for_full_text_search(db_engine: AsyncEngine) -> None:
    """Asserts the plan, not just the index's existence.

    A GIN index that exists but is never chosen is indistinguishable from no
    index at all, and ``alembic check`` reports both as fine.
    """
    async with db_engine.connect() as conn:
        await conn.execute(text("SET enable_seqscan = off"))
        plan = await conn.execute(
            text(
                "EXPLAIN SELECT user_id FROM user_profiles "
                "WHERE to_tsvector('english', coalesce(about_me, '')) "
                "@@ plainto_tsquery('english', 'scholarship')"
            )
        )
        rendered = "\n".join(row[0] for row in plan)

    assert "ix_user_profiles_about_fts" in rendered, rendered
