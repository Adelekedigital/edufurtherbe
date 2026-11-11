"""What `e2a9c4f7b153` changes: an outbox row must name its destination (PRD 30).

The package's ``'posthog'`` default let a row that named none be swept by the
email drain as a message. Without it, a forgotten destination fails at the
insert.
"""

import asyncpg
import pytest
from alembic import command
from tests.integration.test_migrations import ConfigFactory, MigrateTo, execute, scalar

from app.domain.notifications import Channel

pytestmark = pytest.mark.db

REVISION = "e2a9c4f7b153"
PREVIOUS = "c3f81a5d27e4"

UNNAMED = (
    "INSERT INTO outbox_events (event_type, entity_type, entity_id) "
    "VALUES ('credit_spent', 'user', '00000000-0000-7000-8000-0000000000aa')"
)
NAMED = (
    "INSERT INTO outbox_events (event_type, entity_type, entity_id, destination) "
    "VALUES ('credit_spent', 'user', '00000000-0000-7000-8000-0000000000aa', 'analytics')"
)
DESTINATIONS = "SELECT string_agg(destination, ',' ORDER BY destination) FROM outbox_events"


def test_a_row_naming_no_destination_is_refused(
    disposable_database: str, migrate_to: MigrateTo
) -> None:
    migrate_to(disposable_database, REVISION)

    with pytest.raises(asyncpg.NotNullViolationError):
        execute(disposable_database, UNNAMED)


def test_a_row_naming_its_destination_keeps_it(
    disposable_database: str, migrate_to: MigrateTo
) -> None:
    migrate_to(disposable_database, REVISION)

    execute(disposable_database, NAMED)

    assert scalar(disposable_database, DESTINATIONS) == Channel.ANALYTICS


def test_downgrade_restores_the_package_default(
    disposable_database: str, make_alembic_config: ConfigFactory, migrate_to: MigrateTo
) -> None:
    config = make_alembic_config(disposable_database)
    migrate_to(disposable_database, REVISION)
    execute(disposable_database, NAMED)
    command.downgrade(config, PREVIOUS)

    execute(disposable_database, UNNAMED)

    assert scalar(disposable_database, DESTINATIONS) == "analytics,posthog"
