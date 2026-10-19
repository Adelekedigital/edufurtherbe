"""`d2f8b6a4c1e7`: `mentee_goal_schools` (Mentee Settings PRD).

Its own file because `test_migrations.py` is at the size limit (rule 11). Every
constraint has an accepting and a rejecting case: a test that only proves a
constraint refuses garbage cannot tell it from one that refuses everything.
"""

from __future__ import annotations

import json

import asyncpg
import pytest
from alembic import command
from tests.integration.test_migrations import ConfigFactory, execute, scalar

from app.domain.goals import MAX_GOAL_SCHOOLS, SCHOOL_NAME_MAX_LENGTH

pytestmark = pytest.mark.db

SCHOOLS = "d2f8b6a4c1e7"
BEFORE = "e4b9d2a7c1f3"

_GOAL = (
    "WITH u AS (INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
    "VALUES ('{email}', gen_random_uuid(), 'Probe', 'mentee', 'UTC') RETURNING id) "
    "INSERT INTO mentee_goals (user_id) SELECT id FROM u"
)

_SCHOOL = (
    "INSERT INTO mentee_goal_schools (goal_id, school_name_raw, position) "
    "SELECT g.id, {name}, {position} FROM mentee_goals g "
    "JOIN users u ON u.id = g.user_id WHERE u.email = '{email}'"
)


def _goal(url: str, email: str) -> None:
    execute(url, _GOAL.format(email=email))


def _school(url: str, email: str, name: str = "'Harvard'", position: int = 0) -> None:
    execute(url, _SCHOOL.format(email=email, name=name, position=position))


def test_a_goal_holds_ordered_schools_each_with_its_own_id(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    command.upgrade(make_alembic_config(disposable_database), SCHOOLS)
    _goal(disposable_database, "a@example.test")
    _school(disposable_database, "a@example.test", "'Harvard'", 0)
    _school(disposable_database, "a@example.test", f"repeat('x', {SCHOOL_NAME_MAX_LENGTH})", 1)

    assert scalar(disposable_database, "SELECT count(DISTINCT id) FROM mentee_goal_schools") == 2


@pytest.mark.parametrize(
    "name",
    ["''", f"repeat('x', {SCHOOL_NAME_MAX_LENGTH + 1})"],
    ids=["blank", "one-over"],
)
def test_a_school_name_outside_the_bound_is_refused(
    disposable_database: str, make_alembic_config: ConfigFactory, name: str
) -> None:
    command.upgrade(make_alembic_config(disposable_database), SCHOOLS)
    _goal(disposable_database, "a@example.test")

    with pytest.raises(asyncpg.CheckViolationError, match="school_name_raw_length"):
        _school(disposable_database, "a@example.test", name)


def test_the_last_position_under_the_cap_is_accepted(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    command.upgrade(make_alembic_config(disposable_database), SCHOOLS)
    _goal(disposable_database, "a@example.test")
    _school(disposable_database, "a@example.test", position=MAX_GOAL_SCHOOLS - 1)

    assert scalar(disposable_database, "SELECT max(position) FROM mentee_goal_schools") == (
        MAX_GOAL_SCHOOLS - 1
    )


@pytest.mark.parametrize("position", [-1, MAX_GOAL_SCHOOLS], ids=["negative", "past-cap"])
def test_a_position_outside_the_cap_is_refused(
    disposable_database: str, make_alembic_config: ConfigFactory, position: int
) -> None:
    """Bounding the position is what makes the ten-school cap the database's:
    with positions unique per goal, an eleventh school has nowhere to go."""
    command.upgrade(make_alembic_config(disposable_database), SCHOOLS)
    _goal(disposable_database, "a@example.test")

    with pytest.raises(asyncpg.CheckViolationError, match="position_within_cap"):
        _school(disposable_database, "a@example.test", position=position)


def test_two_schools_cannot_share_a_position_on_one_goal(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """The order is total within a goal; another goal may reuse the position."""
    command.upgrade(make_alembic_config(disposable_database), SCHOOLS)
    _goal(disposable_database, "a@example.test")
    _goal(disposable_database, "b@example.test")
    _school(disposable_database, "a@example.test", position=0)
    _school(disposable_database, "b@example.test", position=0)

    with pytest.raises(asyncpg.UniqueViolationError, match="uq_mentee_goal_schools_goal_position"):
        _school(disposable_database, "a@example.test", "'Yale'", 0)


def test_the_schools_go_with_their_goal(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    command.upgrade(make_alembic_config(disposable_database), SCHOOLS)
    _goal(disposable_database, "gone@example.test")
    _goal(disposable_database, "stays@example.test")
    _school(disposable_database, "gone@example.test")
    _school(disposable_database, "stays@example.test")

    execute(
        disposable_database,
        "DELETE FROM mentee_goals WHERE user_id = "
        "(SELECT id FROM users WHERE email = 'gone@example.test')",
    )

    assert scalar(disposable_database, "SELECT count(*) FROM mentee_goal_schools") == 1


def test_a_school_holds_a_catalogue_link_or_none(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """The link is optional: a typed school with no match keeps its name."""
    command.upgrade(make_alembic_config(disposable_database), SCHOOLS)
    _goal(disposable_database, "a@example.test")
    _institution(disposable_database)
    _school(disposable_database, "a@example.test", "'Somewhere Unlisted'", 0)
    execute(
        disposable_database,
        "INSERT INTO mentee_goal_schools (goal_id, institution_id, school_name_raw, position) "
        "SELECT g.id, i.id, 'Probe U', 1 FROM mentee_goals g, institutions i "
        "WHERE i.domain = 'probe.test'",
    )

    assert (
        scalar(
            disposable_database,
            "SELECT string_agg(coalesce(i.name, '-'), ',' ORDER BY s.position) "
            "FROM mentee_goal_schools s LEFT JOIN institutions i ON i.id = s.institution_id",
        )
        == "-,Probe U"
    )


def _institution(url: str) -> None:
    execute(
        url,
        "INSERT INTO institutions (name, domain, source) "
        "VALUES ('Probe U', 'probe.test', 'hipolabs')",
    )


def test_an_institution_a_school_names_cannot_be_deleted(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """`RESTRICT`, as on `education_entries`: a merge repoints, never deletes.
    An institution no school names still deletes."""
    command.upgrade(make_alembic_config(disposable_database), SCHOOLS)
    _goal(disposable_database, "a@example.test")
    execute(
        disposable_database,
        "INSERT INTO institutions (name, domain, source) "
        "VALUES ('Unused U', 'unused.test', 'hipolabs')",
    )
    execute(disposable_database, "DELETE FROM institutions WHERE domain = 'unused.test'")
    _institution(disposable_database)
    execute(
        disposable_database,
        "INSERT INTO mentee_goal_schools (goal_id, institution_id, school_name_raw, position) "
        "SELECT g.id, i.id, 'Probe U', 0 FROM mentee_goals g, institutions i "
        "WHERE i.domain = 'probe.test'",
    )

    with pytest.raises(
        asyncpg.ForeignKeyViolationError, match="fk_mentee_goal_schools_institution"
    ):
        execute(disposable_database, "DELETE FROM institutions WHERE domain = 'probe.test'")


def test_an_update_moves_updated_at(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """The per-table trigger is attached (decision 23)."""
    command.upgrade(make_alembic_config(disposable_database), SCHOOLS)
    _goal(disposable_database, "a@example.test")
    _school(disposable_database, "a@example.test")
    execute(
        disposable_database,
        "UPDATE mentee_goal_schools SET updated_at = '2000-01-01', created_at = '2000-01-01'",
    )
    execute(disposable_database, "UPDATE mentee_goal_schools SET school_name_raw = 'Yale'")

    assert (
        scalar(disposable_database, "SELECT updated_at > created_at FROM mentee_goal_schools")
        is True
    )


def test_the_bounds_are_the_domains(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """One rule, one representation: the migration copies both bounds, so the
    CHECKs it wrote are pinned to the constants the API and model read."""
    command.upgrade(make_alembic_config(disposable_database), "head")

    definitions = scalar(
        disposable_database,
        "SELECT json_object_agg(conname, pg_get_constraintdef(oid))::text FROM pg_constraint "
        "WHERE conrelid = 'mentee_goal_schools'::regclass AND contype = 'c'",
    )
    checks = json.loads(definitions)
    name = checks["ck_mentee_goal_schools_school_name_raw_length"]
    assert f"<= {SCHOOL_NAME_MAX_LENGTH})" in name, name
    position = checks["ck_mentee_goal_schools_position_within_cap"]
    assert f"<= {MAX_GOAL_SCHOOLS - 1})" in position, position


def test_the_downgrade_drops_the_table_and_comes_back(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    config = make_alembic_config(disposable_database)
    command.upgrade(config, SCHOOLS)
    _goal(disposable_database, "a@example.test")
    _school(disposable_database, "a@example.test")

    command.downgrade(config, BEFORE)
    assert scalar(disposable_database, "SELECT to_regclass('mentee_goal_schools')") is None

    command.upgrade(config, SCHOOLS)
    assert scalar(disposable_database, "SELECT count(*) FROM mentee_goal_schools") == 0
