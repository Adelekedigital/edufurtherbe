"""What `8c4e1f7a2d93` does (decision 308): every starter that never expired
gets the end of the month the migration runs in, and the downgrade restores the
old never-expiring starters.

Split from `test_migrations.py` by area, as the file-size limit asks
(non-negotiable 11); the helpers are imported from there, not copied.
"""

import datetime as dt

import pytest
from alembic import command
from tests.integration.test_migrations import ConfigFactory, execute, scalar

from app.domain.credits import end_of_month

pytestmark = pytest.mark.db

REVISION = "8c4e1f7a2d93"
PREVIOUS = "3f9c2a7e1b64"

SEED = (
    "INSERT INTO users (id, auth_id, email, primary_role, timezone) VALUES "
    "('00000000-0000-7000-8000-000000000002', gen_random_uuid(), 'e@example.test', "
    "'mentee', 'UTC'), "
    "('00000000-0000-7000-8000-000000000003', gen_random_uuid(), 'f@example.test', "
    "'mentee', 'UTC');"
    "INSERT INTO credit_lots (user_id, source, quantity_granted, quantity_remaining) VALUES "
    "('00000000-0000-7000-8000-000000000002', 'profile_completed', 1, 1);"
    "INSERT INTO credit_lots "
    "(user_id, source, quantity_granted, quantity_remaining, created_at) VALUES "
    "('00000000-0000-7000-8000-000000000003', 'profile_completed', 1, 0, "
    "'2026-09-10 12:00+00');"
    "INSERT INTO credit_lots (user_id, source, quantity_granted, quantity_remaining, expires_at) "
    "VALUES ('00000000-0000-7000-8000-000000000002', 'admin_grant', 1, 1, NULL)"
)
NULL_STARTERS = (
    "SELECT count(*) FROM credit_lots WHERE source = 'profile_completed' AND expires_at IS NULL"
)
#: Each starter's expiry, the held one first.
STARTER_EXPIRIES = (
    "SELECT array_agg(expires_at ORDER BY quantity_remaining DESC) "
    "FROM credit_lots WHERE source = 'profile_completed'"
)
OTHER_NULLS = (
    "SELECT count(*) FROM credit_lots WHERE source <> 'profile_completed' AND expires_at IS NULL"
)


def test_a_held_starter_gets_this_month_and_a_spent_one_its_own(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """The held starter is this period's level-1 credit; the spent one, from
    September, dies with September — given this month's end it would count as
    this month's payment and leave the month owing nothing."""
    config = make_alembic_config(disposable_database)
    command.upgrade(config, PREVIOUS)
    execute(disposable_database, SEED)

    command.upgrade(config, REVISION)

    assert scalar(disposable_database, NULL_STARTERS) == 0
    assert scalar(disposable_database, STARTER_EXPIRIES) == [
        end_of_month(dt.datetime.now(dt.UTC)),
        dt.datetime(2026, 10, 1, tzinfo=dt.UTC),
    ]
    # Only starters: any other lot keeps whatever it had.
    assert scalar(disposable_database, OTHER_NULLS) == 1


def test_the_downgrade_restores_never_expiring_starters(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    config = make_alembic_config(disposable_database)
    command.upgrade(config, PREVIOUS)
    execute(disposable_database, SEED)
    command.upgrade(config, REVISION)

    command.downgrade(config, PREVIOUS)

    assert scalar(disposable_database, NULL_STARTERS) == 2
    command.upgrade(config, REVISION)
    assert scalar(disposable_database, NULL_STARTERS) == 0
