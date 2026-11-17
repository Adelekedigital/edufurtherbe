"""What the visitor day-key migration leaves behind (decision 348).

Split from `test_migrations.py` by area, as the file-size limit asks
(non-negotiable 11); the helpers are imported from there, not copied.
"""

import asyncpg
import pytest
from alembic import command
from tests.integration.test_migrations import ConfigFactory, MigrateTo, execute, scalar

pytestmark = pytest.mark.db

REVISION = "a646dd306ac5"
PREVIOUS = "c569432b779f"


def day_key(day: str = "2026-11-23", length: int = 32) -> str:
    return (
        "INSERT INTO visitor_day_keys (day, key) "  # noqa: S608
        f"VALUES ('{day}', gen_random_bytes({length}))"
    )


def test_one_random_key_per_day(disposable_database: str, migrate_to: MigrateTo) -> None:
    migrate_to(disposable_database, REVISION)

    execute(disposable_database, day_key())
    assert scalar(
        disposable_database,
        "SELECT id IS NOT NULL AND created_at IS NOT NULL AND octet_length(key) = 32 "
        "FROM visitor_day_keys",
    )
    with pytest.raises(asyncpg.UniqueViolationError, match="uq_visitor_day_keys_day"):
        execute(disposable_database, day_key())
    execute(disposable_database, day_key(day="2026-11-24"))
    assert scalar(disposable_database, "SELECT count(*) FROM visitor_day_keys") == 2


@pytest.mark.parametrize("length", [31, 33])
def test_a_key_is_exactly_32_bytes(
    disposable_database: str, migrate_to: MigrateTo, length: int
) -> None:
    migrate_to(disposable_database, REVISION)

    with pytest.raises(asyncpg.CheckViolationError, match="ck_visitor_day_keys_key_is_32_bytes"):
        execute(disposable_database, day_key(length=length))


def test_the_downgrade_drops_the_table(
    disposable_database: str, make_alembic_config: ConfigFactory, migrate_to: MigrateTo
) -> None:
    migrate_to(disposable_database, REVISION)
    execute(disposable_database, day_key())

    command.downgrade(make_alembic_config(disposable_database), PREVIOUS)

    assert scalar(disposable_database, "SELECT to_regclass('visitor_day_keys')") is None
