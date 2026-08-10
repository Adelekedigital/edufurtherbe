"""The seven profile tables, loaded from a legacy snapshot.

Synthetic records rather than the dev export: ``script-data-dev/`` is gitignored
because it holds real people, so CI does not have it and a suite that read it
would skip there and pass here.

That is not only a packaging concern. Several behaviours this loader must get
right **cannot be reached** by the dev data at all — no attached mentor in it is
unlisted, and every award year is inside the CHECK except one. A fixture is the
only thing that can hold them.
"""

from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.domain.resolve import COUNTRY_ALIASES, resolve_names
from app.domain.transform import plan_identity
from app.domain.transform.profiles import plan_profiles
from app.infra.db.triggers import timestamps_from_source_across
from app.infra.etl.loader import UserLoader
from app.infra.etl.profiles import ProfileCounts, ProfileLoader, lookup_maps
from app.infra.etl.reconcile import reconcile_profiles
from app.infra.etl.satellites import reference_maps, user_id_map
from conftest import USER_ID, user

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

MENTOR_THING = "1700000000000x000000000000000010"
EDUCATION_THING = "1700000000000x000000000000000020"
GOAL_THING = "1700000000000x000000000000000030"
AWARD_THING = "1700000000000x000000000000000040"
SERVICE_THING = "1700000000000x000000000000000050"

NY = ZoneInfo("America/New_York")
THIS_YEAR = 2026

TABLES = (
    "mentor_profiles",
    "mentee_goals",
    "mentor_service_offerings",
    "mentee_goal_countries",
    "mentee_goal_needs",
    "education_entries",
    "user_awards",
)


def thing(bubble_id: str, **fields: Any) -> dict[str, Any]:
    """A canonical Thing record, owned by ``USER_ID``."""
    return {
        "bubble_id": bubble_id,
        "created_at": "2024-03-01T09:00:00.000Z",
        "modified_at": "2024-06-02T10:30:00.000Z",
        "Creator": USER_ID,
        **fields,
    }


def mentor_record(**fields: Any) -> dict[str, Any]:
    return thing(
        MENTOR_THING,
        **{
            "✅approvedText": "Yes",
            "availableStatus": "yes",
            "meetingVenueSelection": "Edufurther Video (Recommended)",
            "mentorMentorshipSupport(listText)": "Test Preparation , Document Review",
            **fields,
        },
    )


def education_record(**fields: Any) -> dict[str, Any]:
    return thing(
        EDUCATION_THING,
        **{
            "schoolName": "University of Lagos",
            "degreeCategory": "Masters",
            "dateStart": "Jan 1, 2020 12:00 am",
            "dateEnd": "Dec 31, 2022 11:30 pm",
            **fields,
        },
    )


def goal_record(**fields: Any) -> dict[str, Any]:
    return thing(
        GOAL_THING,
        **{
            "degreeGoal(text)": "MSc (Master of Science)",
            "Mentorship Goals(Text)": "test preparation , school selection",
            **fields,
        },
    )


def award_record(**fields: Any) -> dict[str, Any]:
    return thing(
        AWARD_THING,
        **{
            "Award-institution": "Oxford",
            "Award-title": "Chevening",
            "Award-year": "2024",
            **fields,
        },
    )


def linked_user(**overrides: Any) -> dict[str, Any]:
    """A user pointing at one of each Thing — the join direction that matters."""
    return user(
        **{
            # The shared fixture points at a `PersonalInfo` record, and
            # `plan_identity` refuses a link it cannot resolve. These tests supply
            # no profiles, so the link is cleared rather than the refusal
            # weakened — that check is doing exactly its job.
            "👤Personal Info": None,
            "Mentor": MENTOR_THING,
            "📚Education": EDUCATION_THING,
            "member goal": GOAL_THING,
            "mentor service": SERVICE_THING,
            **overrides,
        }
    )


async def load_everything(
    conn: AsyncConnection,
    users: list[dict[str, Any]],
    *,
    mentors: list[dict[str, Any]] | None = None,
    education: list[dict[str, Any]] | None = None,
    goals: list[dict[str, Any]] | None = None,
    awards: list[dict[str, Any]] | None = None,
    services: list[dict[str, Any]] | None = None,
) -> tuple[Any, ProfileCounts]:
    """Identity first, then profiles — the order production runs them in."""
    identity = plan_identity(users, [])
    assert identity.ok, identity.errors
    await UserLoader(conn).load(identity.users)

    plan = plan_profiles(
        users,
        education_records=education or [],
        goal_records=goals or [],
        service_records=services or [],
        mentor_records=mentors or [],
        award_records=awards or [],
        # Required, and not only for the offsetless date fields: it is the lens a
        # `date` column is read through. An ISO timestamp carrying its own offset
        # ignores it, so one zone serves both shapes.
        export_timezone=NY,
        this_year=THIS_YEAR,
    )
    assert plan.ok, plan.errors

    ids = await user_id_map(conn)
    country_ref, _ = await reference_maps(conn)
    offerings, degrees = await lookup_maps(conn)

    async with timestamps_from_source_across(conn, TABLES):
        counts = await ProfileLoader(conn).load(
            users=ids,
            mentors=plan.mentors,
            education=plan.education,
            awards=plan.awards,
            goals=plan.goals,
            offerings=offerings,
            degrees=degrees,
            countries=resolve_names(plan.country_names(), country_ref, COUNTRY_ALIASES),
        )
    return plan, counts


# --------------------------------------------------------------------------
# The happy path, end to end
# --------------------------------------------------------------------------


async def test_all_seven_tables_load_from_one_snapshot(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        _, counts = await load_everything(
            conn,
            [linked_user()],
            mentors=[mentor_record()],
            education=[education_record()],
            goals=[goal_record()],
            awards=[award_record()],
        )

        stored = await conn.execute(
            text(
                "SELECT (SELECT count(*) FROM mentor_profiles), "
                "(SELECT count(*) FROM mentor_service_offerings), "
                "(SELECT count(*) FROM education_entries), "
                "(SELECT count(*) FROM user_awards), "
                "(SELECT count(*) FROM mentee_goals), "
                "(SELECT count(*) FROM mentee_goal_needs)"
            )
        )

    assert stored.one() == (1, 2, 1, 1, 1, 2)

    # The **reported** counts, asserted separately from the rows they describe.
    # A mutation batch found this missing: inflating `mentor_services` left every
    # row assertion green, because `ON CONFLICT` makes the database agree with
    # itself whatever the loader claims. Reconciliation is only worth running if
    # its numbers are true, and this is the only thing that checks they are.
    assert (counts.mentor_profiles, counts.mentor_services) == (1, 2)
    assert (counts.education, counts.awards, counts.goals) == (1, 1, 1)
    assert (counts.goal_needs, counts.goal_countries) == (2, 0)


async def test_two_columns_are_deliberately_never_written(db_engine: AsyncEngine) -> None:
    """Both are claims the loader makes about itself, so both are asserted.

    ``custom_meeting_url`` stays null because the legacy ``meetingVenueLink`` is
    residue — selecting a venue auto-created a per-session link that lived on the
    session record. Writing it would carry a static personal room forward, which
    the package calls a privacy incident, and would violate the CHECK on every
    non-``custom`` row besides.

    ``institution_id`` stays null because matching is a separate, re-runnable
    pass (settled decision 61). A loader that quietly started filling either
    would pass every other test in this file.
    """
    async with db_engine.begin() as conn:
        await load_everything(
            conn,
            [linked_user()],
            mentors=[mentor_record(**{"🔗meetingVenueLink": "https://meet.google.com/abc-defg"})],
            education=[education_record()],
        )
        stored = await conn.execute(
            text(
                "SELECT (SELECT count(*) FROM mentor_profiles "
                "        WHERE custom_meeting_url IS NOT NULL), "
                "(SELECT count(*) FROM education_entries WHERE institution_id IS NOT NULL), "
                "(SELECT count(*) FROM education_entries WHERE school_name_raw IS NOT NULL)"
            )
        )

    # The third is the positive half: null links, but never a null raw name.
    assert stored.one() == (0, 0, 1)


async def test_a_second_load_changes_nothing(db_engine: AsyncEngine) -> None:
    """Idempotent across all seven, on three different anchor kinds.

    Four tables key on ``legacy_bubble_id``, two on ``user_id``, three on their
    natural pair — so "it re-runs cleanly" is three separate claims.
    """
    payload: dict[str, Any] = {
        "mentors": [mentor_record()],
        "education": [education_record()],
        "goals": [goal_record()],
        "awards": [award_record()],
    }
    async with db_engine.begin() as conn:
        await load_everything(conn, [linked_user()], **payload)

    async with db_engine.begin() as conn:
        await load_everything(conn, [linked_user()], **payload)
        stored = await conn.execute(
            text(
                "SELECT (SELECT count(*) FROM mentor_profiles), "
                "(SELECT count(*) FROM mentor_service_offerings), "
                "(SELECT count(*) FROM education_entries), "
                "(SELECT count(*) FROM user_awards), "
                "(SELECT count(*) FROM mentee_goals), "
                "(SELECT count(*) FROM mentee_goal_needs)"
            )
        )

    assert stored.one() == (1, 2, 1, 1, 1, 2), "a re-run duplicated rows"


async def test_bubble_timestamps_survive_the_load(db_engine: AsyncEngine) -> None:
    """The check that catches a load which ran with the trigger enabled.

    ``trg_set_updated_at`` is unconditional, so without the disable every
    migrated ``updated_at`` becomes the import clock — silently, with row counts
    and null-rate checks all still passing.
    """
    async with db_engine.begin() as conn:
        await load_everything(
            conn, [linked_user()], mentors=[mentor_record()], education=[education_record()]
        )
        stored = await conn.execute(
            text(
                "SELECT updated_at FROM mentor_profiles "
                "UNION SELECT updated_at FROM education_entries"
            )
        )
        years = {row[0].year for row in stored}

    assert years == {2024}, "an updated_at was stamped by the importer"


async def test_reconciliation_passes_on_a_clean_load(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        plan, counts = await load_everything(
            conn,
            [linked_user()],
            mentors=[mentor_record()],
            education=[education_record()],
            goals=[goal_record()],
            awards=[award_record()],
        )
        result = await reconcile_profiles(conn, plan, counts)

    assert result.ok, result.report()
    # Named, not inferred from a zero — the whole point of reporting it.
    assert "mentee_goal_countries" in result.empty


# --------------------------------------------------------------------------
# What the dev export cannot reach
# --------------------------------------------------------------------------


async def test_an_award_year_outside_the_check_loads_as_null(db_engine: AsyncEngine) -> None:
    """The award survives; only the year is dropped, and it is reported by id."""
    async with db_engine.begin() as conn:
        plan, _ = await load_everything(
            conn, [linked_user()], awards=[award_record(**{"Award-year": "2029"})]
        )
        stored = await conn.execute(text("SELECT title, year FROM user_awards"))

    assert stored.one() == ("Chevening", None)
    assert plan.rejected_award_years == (f"{AWARD_THING}: 2029",)


async def test_an_unlisted_mentor_lands_with_an_explicit_reason(db_engine: AsyncEngine) -> None:
    """Not reachable from the export: both unlisted rows there are orphan shells.

    The column default is ``never_approved``, which is wrong for a mentor who was
    approved and then paused. Asserted against the **database**, not the plan,
    because the default is what would silently win.
    """
    async with db_engine.begin() as conn:
        await load_everything(
            conn,
            [linked_user()],
            mentors=[mentor_record(**{"✅approvedText": "Yes", "availableStatus": "no"})],
        )
        stored = await conn.execute(
            text("SELECT approval_status::text, listing_status::text FROM mentor_profiles")
        )
        # The reason moved to the log: it describes a *transition*, and on the
        # profile it could only ever describe the most recent one.
        seeded = await conn.execute(
            text(
                "SELECT status_type::text, reason FROM mentor_status_events "
                "WHERE status_type = 'unlisted'"
            )
        )

    assert stored.one() == ("approved", "unlisted")
    assert seeded.one() == ("unlisted", "mentor_paused")


async def test_a_pending_mentor_is_loaded_and_gets_no_history(db_engine: AsyncEngine) -> None:
    """**The state every fixture skipped, and the loader could not survive.**

    ``approvedText`` blank maps to `pending`, which is the default and the
    common case in the export. The loader seeded a status event straight from
    `approval_status`, but `mentor_status_type` has no `pending` member — so the
    whole load raised `InvalidTextRepresentationError` and, being one
    transaction, wrote nothing at all. Every existing fixture passes ``"Yes"``,
    so the defect lived exactly where no test looked.

    No event rather than a `pending` one is not merely what the enum permits: it
    is what the migration's backfill already does, twice, with
    ``WHERE approval_status <> 'pending'`` — because writing a decision row for
    a mentor nobody has decided on fabricates a decision nobody made.
    """
    async with db_engine.begin() as conn:
        await load_everything(
            conn,
            [linked_user()],
            mentors=[mentor_record(**{"✅approvedText": ""})],
        )
        stored = await conn.execute(text("SELECT approval_status::text FROM mentor_profiles"))
        events = await conn.execute(text("SELECT count(*) FROM mentor_status_events"))

    assert stored.one() == ("pending",), "the profile itself must still load"
    assert events.scalar_one() == 0, "a pending mentor has no decision to record"


async def test_an_approved_mentor_still_gets_both_events(db_engine: AsyncEngine) -> None:
    """The other half of the guard, so it cannot be satisfied by seeding nothing.

    A guard that skipped every mentor would pass the test above and destroy the
    history for the mentors who have one.
    """
    async with db_engine.begin() as conn:
        await load_everything(
            conn,
            [linked_user()],
            mentors=[mentor_record(**{"✅approvedText": "Yes"})],
        )
        events = await conn.execute(
            text("SELECT status_type::text FROM mentor_status_events ORDER BY status_type")
        )

    assert [row[0] for row in events] == ["approved", "listed"]


async def test_history_appears_when_a_later_export_decides_a_pending_mentor(
    db_engine: AsyncEngine,
) -> None:
    """The guard must not outlive the condition that justified it.

    "Already has history" and "is pending" are two different reasons to skip, and
    a re-run after the mentor is approved has to seed the history the first run
    correctly withheld. Ordering the conditions the other way would leave that
    mentor permanently without a log.
    """
    async with db_engine.begin() as conn:
        await load_everything(
            conn, [linked_user()], mentors=[mentor_record(**{"✅approvedText": ""})]
        )
        first = await conn.execute(text("SELECT count(*) FROM mentor_status_events"))
        assert first.scalar_one() == 0

        await load_everything(
            conn, [linked_user()], mentors=[mentor_record(**{"✅approvedText": "Yes"})]
        )
        after = await conn.execute(
            text("SELECT status_type::text FROM mentor_status_events ORDER BY status_type")
        )

    assert [row[0] for row in after] == ["approved", "listed"], (
        "a mentor decided after the first load must gain their history on the next one"
    )


async def test_a_mentee_with_a_mentor_link_still_gets_a_profile(db_engine: AsyncEngine) -> None:
    """Legacy disagrees with itself and D2 settles it.

    One user in the extract has ``Role = Mentee`` and a linked Mentor record.
    ``primary_role`` is a UX hint; authorization is profile existence. So the
    profile is created and the role is left alone.
    """
    async with db_engine.begin() as conn:
        await load_everything(
            conn, [linked_user(**{"👥Role": "Mentee"})], mentors=[mentor_record()]
        )
        stored = await conn.execute(
            text(
                "SELECT u.primary_role::text, count(m.id) FROM users u "
                "LEFT JOIN mentor_profiles m ON m.user_id = u.id GROUP BY 1"
            )
        )

    assert stored.one() == ("mentee", 1)


async def test_a_thing_no_user_points_at_is_not_loaded(db_engine: AsyncEngine) -> None:
    """The three orphan mentor rows in the extract are this case.

    Reported and counted, never attributed — and never silently dropped either,
    which is the distinction that matters at cutover.
    """
    async with db_engine.begin() as conn:
        plan, _ = await load_everything(
            conn, [linked_user(**{"Mentor": None})], mentors=[mentor_record()]
        )
        stored = await conn.execute(text("SELECT count(*) FROM mentor_profiles"))

    assert stored.scalar_one() == 0
    assert plan.unattached["mentor_profiles"] == (MENTOR_THING,)


async def test_education_dates_land_as_written(db_engine: AsyncEngine) -> None:
    """``Dec 31 … 11:30 pm`` must not become 1 January.

    The dev export cannot express this — every date in it is exactly midnight,
    whose UTC date agrees with the local one.
    """
    async with db_engine.begin() as conn:
        await load_everything(conn, [linked_user()], education=[education_record()])
        stored = await conn.execute(
            text("SELECT date_start::text, date_end::text FROM education_entries")
        )

    assert stored.one() == ("2020-01-01", "2022-12-31")
