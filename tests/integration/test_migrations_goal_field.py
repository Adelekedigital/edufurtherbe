"""The migration that gives a mentee goal its field of study (decision 259).

Its own file because `test_migrations.py` is at the size limit (rule 11).
"""

from __future__ import annotations

import asyncpg
import pytest
from alembic import command
from tests.integration.test_migrations import ConfigFactory, execute, scalar

pytestmark = pytest.mark.db


_GOAL = (
    "WITH u AS (INSERT INTO users (email, auth_id, first_name, primary_role, timezone) "
    "VALUES ('{email}', gen_random_uuid(), 'Probe', 'mentee', 'UTC') RETURNING id) "
    "INSERT INTO mentee_goals (user_id, field_of_study) SELECT id, {field} FROM u"
)


def test_a_goal_holds_a_field_of_study_of_one_to_a_hundred_characters(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """`e4710b359d1b` adds `mentee_goals.field_of_study` (Onboarding v5 B1)."""
    command.upgrade(make_alembic_config(disposable_database), "e4710b359d1b")
    execute(disposable_database, _GOAL.format(email="none@example.test", field="NULL"))
    execute(disposable_database, _GOAL.format(email="cs@example.test", field="'Computer Science'"))
    execute(disposable_database, _GOAL.format(email="long@example.test", field="repeat('y', 100)"))

    assert scalar(disposable_database, "SELECT count(field_of_study) FROM mentee_goals") == 2


@pytest.mark.parametrize("value", ["''", "repeat('y', 101)"], ids=["blank", "too-long"])
def test_a_blank_or_overlong_field_of_study_is_refused(
    disposable_database: str, make_alembic_config: ConfigFactory, value: str
) -> None:
    command.upgrade(make_alembic_config(disposable_database), "e4710b359d1b")

    with pytest.raises(asyncpg.CheckViolationError):
        execute(disposable_database, _GOAL.format(email="bad@example.test", field=value))


def test_the_field_of_study_downgrade_refuses_while_a_goal_holds_one(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """Dropping the column would delete what mentees typed, so a downgrade over
    values stops and says so; without values it drops the column and comes back."""
    config = make_alembic_config(disposable_database)
    command.upgrade(config, "e4710b359d1b")
    execute(disposable_database, _GOAL.format(email="cs@example.test", field="'Law'"))

    with pytest.raises(RuntimeError, match="field of study"):
        command.downgrade(config, "d7b2f4a8c615")

    execute(disposable_database, "UPDATE mentee_goals SET field_of_study = NULL")
    command.downgrade(config, "d7b2f4a8c615")
    assert (
        scalar(
            disposable_database,
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'mentee_goals' AND column_name = 'field_of_study'",
        )
        == 0
    )
    command.upgrade(config, "e4710b359d1b")
