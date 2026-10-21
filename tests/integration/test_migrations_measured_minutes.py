"""What `c3e8a1f5d29b` leaves behind, beyond "the chain runs".

Split from `test_migrations.py` by area, as the file-size limit asks
(non-negotiable 11); the helpers are imported from there, not copied.
"""

import pytest
from alembic import command
from tests.integration.test_migrations import ConfigFactory, scalar

pytestmark = pytest.mark.db

COLUMN = (
    "SELECT count(*) FROM information_schema.columns "
    "WHERE table_name = 'sessions' AND column_name = 'measured_minutes'"
)


def test_measured_minutes_is_nullable_and_never_negative(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """Null is "not measured" and every existing row stays null, so the stats
    keep today's figure; the table refuses a negative count."""
    config = make_alembic_config(disposable_database)
    command.upgrade(config, "c3e8a1f5d29b")

    assert scalar(disposable_database, COLUMN) == 1
    assert (
        scalar(
            disposable_database,
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'sessions' AND column_name = 'measured_minutes'",
        )
        == "YES"
    )
    assert (
        scalar(
            disposable_database,
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_sessions_measured_minutes_not_negative'",
        )
        == "CHECK (((measured_minutes IS NULL) OR (measured_minutes >= 0)))"
    )


def test_the_downgrade_removes_the_column(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    config = make_alembic_config(disposable_database)
    command.upgrade(config, "c3e8a1f5d29b")

    command.downgrade(config, "b2d6f9a1c4e7")

    assert scalar(disposable_database, COLUMN) == 0
    command.upgrade(config, "c3e8a1f5d29b")
    assert scalar(disposable_database, COLUMN) == 1
