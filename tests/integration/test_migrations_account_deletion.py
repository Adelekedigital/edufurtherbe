"""The migration for deleting your own account (Mentee Settings PRD): the purge
date's guard and the `account_deleted` reason code.

Its own file because `test_migrations.py` is at the size limit (rule 11).
"""

from __future__ import annotations

import asyncpg
import pytest
import sqlalchemy.exc
from alembic import command
from tests.integration.test_migrations import ConfigFactory, execute, scalar

pytestmark = pytest.mark.db

REVISION = "e4b9d2a7c1f3"
PREVIOUS = "c7d4a2e9f1b6"

_USER = """
    INSERT INTO users (email, auth_id, first_name, primary_role, timezone, deleted_at)
    VALUES ('purge-{tag}@example.test', gen_random_uuid(), 'P', 'mentee', 'UTC', {deleted_at})
"""
_PURGE_AT = "UPDATE users SET deletion_purge_at = {at} WHERE email = 'purge-{tag}@example.test'"

_SESSION = """
    WITH m AS (
        INSERT INTO users (email, auth_id, first_name, primary_role, timezone)
        VALUES ('reason-m@example.test', gen_random_uuid(), 'M', 'mentor', 'UTC') RETURNING id
    ), e AS (
        INSERT INTO users (email, auth_id, first_name, primary_role, timezone)
        VALUES ('reason-e@example.test', gen_random_uuid(), 'E', 'mentee', 'UTC') RETURNING id
    )
    INSERT INTO sessions (mentor_id, mentee_id, starts_at, duration_minutes, status)
    SELECT m.id, e.id, '2026-11-02 10:00+00', 60, 'cancelled' FROM m CROSS JOIN e
"""
_ACCOUNT_DELETED_EVENT = """
    INSERT INTO session_events (session_id, from_status, to_status, actor_type, reason_code)
    SELECT id, 'confirmed', 'cancelled', 'user', 'account_deleted' FROM sessions
"""


def test_a_purge_date_must_follow_a_deletion(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """The guard applies over rows that exist before it, then refuses a purge
    date on a live account or one not after the deletion, either of which the
    purge job would delete early. A date after the deletion is accepted."""
    config = make_alembic_config(disposable_database)
    command.upgrade(config, PREVIOUS)
    execute(disposable_database, _USER.format(tag="live", deleted_at="NULL"))
    execute(disposable_database, _USER.format(tag="deleted", deleted_at="now()"))

    command.upgrade(config, REVISION)

    execute(disposable_database, _PURGE_AT.format(at="now() + interval '30 days'", tag="deleted"))
    for tag, at in (("live", "now() + interval '30 days'"), ("deleted", "deleted_at")):
        with pytest.raises(asyncpg.CheckViolationError, match="deletion_purge_follows_deleted"):
            execute(disposable_database, _PURGE_AT.format(at=at, tag=tag))
    assert (
        scalar(
            disposable_database, "SELECT count(*) FROM users WHERE deletion_purge_at IS NOT NULL"
        )
        == 1
    )


def test_the_downgrade_refuses_while_an_account_awaits_its_purge(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """Dropping the column would leave a deletion its owner asked for unpurged
    forever. With none pending, the column and its guard go."""
    config = make_alembic_config(disposable_database)
    command.upgrade(config, REVISION)
    execute(disposable_database, _USER.format(tag="deleted", deleted_at="now()"))
    execute(disposable_database, _PURGE_AT.format(at="now() + interval '30 days'", tag="deleted"))

    with pytest.raises(RuntimeError, match="1 deleted account"):
        command.downgrade(config, PREVIOUS)

    execute(disposable_database, _PURGE_AT.format(at="NULL", tag="deleted"))
    command.downgrade(config, PREVIOUS)
    assert (
        scalar(
            disposable_database,
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'users' AND column_name = 'deletion_purge_at'",
        )
        == 0
    )
    assert (
        scalar(
            disposable_database,
            "SELECT count(*) FROM pg_constraint "
            "WHERE conname = 'ck_users_deletion_purge_follows_deleted'",
        )
        == 0
    )


def test_the_account_deleted_reason_code_and_back(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """The migration admits `account_deleted`, and its downgrade refuses while an
    event carries it rather than rewriting the code (the `other` precedent)."""
    config = make_alembic_config(disposable_database)
    command.upgrade(config, PREVIOUS)
    execute(disposable_database, _SESSION)
    with pytest.raises(asyncpg.CheckViolationError, match="reason_code_is_known"):
        execute(disposable_database, _ACCOUNT_DELETED_EVENT)

    command.upgrade(config, REVISION)
    execute(disposable_database, _ACCOUNT_DELETED_EVENT)

    with pytest.raises(sqlalchemy.exc.IntegrityError, match="reason_code_is_known"):
        command.downgrade(config, PREVIOUS)
    execute(disposable_database, "DELETE FROM session_events WHERE reason_code = 'account_deleted'")
    command.downgrade(config, PREVIOUS)
