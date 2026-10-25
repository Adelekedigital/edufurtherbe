"""What `7c41e9b2a6d3` leaves behind, beyond "the chain runs".

Split from `test_migrations.py` by area, as the file-size limit asks
(non-negotiable 11); the helpers are imported from there, not copied.
"""

import pytest
from alembic import command
from tests.integration.test_migrations import ConfigFactory, scalar

pytestmark = pytest.mark.db

REVISION = "7c41e9b2a6d3"
PREVIOUS = "d9a3f6b1c2e8"

COLUMN = (
    "SELECT count(*) FROM information_schema.columns "
    "WHERE table_name = 'sessions' AND column_name = 'reminder_sent_at'"
)


def test_reminder_sent_at_is_a_nullable_instant(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """Null is "no reminder sent", which is true of every existing row."""
    config = make_alembic_config(disposable_database)
    command.upgrade(config, REVISION)

    assert scalar(disposable_database, COLUMN) == 1
    assert scalar(
        disposable_database,
        "SELECT is_nullable || ':' || data_type FROM information_schema.columns "
        "WHERE table_name = 'sessions' AND column_name = 'reminder_sent_at'",
    ) == ("YES:timestamp with time zone")


def test_the_downgrade_removes_the_column(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    config = make_alembic_config(disposable_database)
    command.upgrade(config, REVISION)

    command.downgrade(config, PREVIOUS)

    assert scalar(disposable_database, COLUMN) == 0
    command.upgrade(config, REVISION)
    assert scalar(disposable_database, COLUMN) == 1
