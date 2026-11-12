"""What the activity-days migration leaves behind (decision 342).

Split from `test_migrations.py` by area, as the file-size limit asks
(non-negotiable 11); the helpers are imported from there, not copied.
"""

import asyncpg
import pytest
from alembic import command
from tests.integration.test_migrations import ConfigFactory, MigrateTo, execute, scalar

pytestmark = pytest.mark.db

REVISION = "d4e7a1c95b26"
PREVIOUS = "f6b1d3a8c925"

#: One person, id written into the statements: these are fixed test strings.
SEED = (
    "INSERT INTO users (id, auth_id, email, primary_role, timezone) VALUES "
    "('00000000-0000-7000-8000-000000000042', gen_random_uuid(), "
    "'active@example.test', 'mentee', 'UTC')"
)
ACTIVE = (
    "INSERT INTO user_activity_days (user_id, day) "
    "VALUES ('00000000-0000-7000-8000-000000000042', '2026-11-11')"
)
NEXT_DAY = ACTIVE.replace("2026-11-11", "2026-11-12")
DELETE_PERSON = "DELETE FROM users WHERE id = '00000000-0000-7000-8000-000000000042'"
ROWS = "SELECT count(*) FROM user_activity_days"


def test_one_row_per_person_per_day_going_with_the_person(
    disposable_database: str, migrate_to: MigrateTo
) -> None:
    migrate_to(disposable_database, REVISION)
    execute(disposable_database, SEED)

    execute(disposable_database, ACTIVE)
    assert scalar(
        disposable_database,
        "SELECT id IS NOT NULL AND created_at IS NOT NULL FROM user_activity_days",
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        execute(disposable_database, ACTIVE)
    execute(disposable_database, NEXT_DAY)
    assert scalar(disposable_database, ROWS) == 2

    execute(disposable_database, DELETE_PERSON)
    assert scalar(disposable_database, ROWS) == 0


def test_the_downgrade_drops_the_table(
    disposable_database: str, make_alembic_config: ConfigFactory, migrate_to: MigrateTo
) -> None:
    migrate_to(disposable_database, REVISION)
    execute(disposable_database, SEED)
    execute(disposable_database, ACTIVE)

    command.downgrade(make_alembic_config(disposable_database), PREVIOUS)

    assert scalar(disposable_database, "SELECT to_regclass('user_activity_days')") is None
