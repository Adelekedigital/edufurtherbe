"""What `abee7fed06b7` changes: one reminder per kind per recipient **per channel**.

A push row for the five-minute reminder carries the same session, kind and
recipient as its email row. Under the old index the second insert conflicted
and `ON CONFLICT DO NOTHING` dropped it in silence (decision 312).
"""

import asyncpg
import pytest
from alembic import command
from sqlalchemy.exc import IntegrityError
from tests.integration.test_migrations import ConfigFactory, MigrateTo, execute, scalar

from app.domain.notifications import Channel

pytestmark = pytest.mark.db

REVISION = "abee7fed06b7"
PREVIOUS = "5b2e9d41c7a3"

SESSION = "00000000-0000-7000-8000-0000000000aa"
RECIPIENT = "00000000-0000-7000-8000-000000000001"
ROW = (
    "INSERT INTO outbox_events (event_type, entity_type, entity_id, destination, payload) "
    "VALUES ('session_last_reminder', 'session', '{session}', '{destination}', "
    '\'{{"recipient_id": "{recipient}", "kind": "m5"}}\')'
)
COUNT = "SELECT count(*) FROM outbox_events"


def row(destination: Channel) -> str:
    return ROW.format(session=SESSION, destination=destination, recipient=RECIPIENT)


def test_an_email_and_a_push_for_one_reminder_both_stay(
    disposable_database: str, migrate_to: MigrateTo
) -> None:
    migrate_to(disposable_database, REVISION)

    execute(disposable_database, row(Channel.EMAIL))
    execute(disposable_database, row(Channel.PUSH))

    assert scalar(disposable_database, COUNT) == 2


def test_the_same_reminder_twice_on_one_channel_is_still_refused(
    disposable_database: str, migrate_to: MigrateTo
) -> None:
    migrate_to(disposable_database, REVISION)
    execute(disposable_database, row(Channel.PUSH))

    with pytest.raises(asyncpg.UniqueViolationError):
        execute(disposable_database, row(Channel.PUSH))


def test_downgrade_restores_the_index_without_the_channel(
    disposable_database: str, make_alembic_config: ConfigFactory, migrate_to: MigrateTo
) -> None:
    config = make_alembic_config(disposable_database)
    migrate_to(disposable_database, REVISION)
    command.downgrade(config, PREVIOUS)
    execute(disposable_database, row(Channel.EMAIL))

    with pytest.raises(asyncpg.UniqueViolationError):
        execute(disposable_database, row(Channel.PUSH))


def test_downgrade_fails_and_keeps_both_rows_when_a_reminder_is_on_two_channels(
    disposable_database: str, make_alembic_config: ConfigFactory, migrate_to: MigrateTo
) -> None:
    config = make_alembic_config(disposable_database)
    migrate_to(disposable_database, REVISION)
    execute(disposable_database, row(Channel.EMAIL))
    execute(disposable_database, row(Channel.PUSH))

    with pytest.raises(IntegrityError):
        command.downgrade(config, PREVIOUS)

    assert scalar(disposable_database, COUNT) == 2
