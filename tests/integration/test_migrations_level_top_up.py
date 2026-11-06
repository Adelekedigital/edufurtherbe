"""What `3f9c2a7e1b64` does (decision 308): `level_top_up` joins the source
check, and the downgrade refuses while a lot carries it.

Split from `test_migrations.py` by area, as the file-size limit asks
(non-negotiable 11); the helpers are imported from there, not copied.
"""

import pytest
from alembic import command
from tests.integration.test_migrations import ConfigFactory, execute, scalar

pytestmark = pytest.mark.db

REVISION = "3f9c2a7e1b64"
PREVIOUS = "a7d3e5c91b42"

SEED = (
    "INSERT INTO users (id, auth_id, email, primary_role, timezone) VALUES "
    "('00000000-0000-7000-8000-000000000002', gen_random_uuid(), 'e@example.test', "
    "'mentee', 'UTC')"
)
TOP_UP = (
    "INSERT INTO credit_lots (user_id, source, quantity_granted, quantity_remaining) "
    "VALUES ('00000000-0000-7000-8000-000000000002', 'level_top_up', 1, 1)"
)
DEFINITION = (
    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
    "WHERE conname = 'ck_credit_lots_source_is_known'"
)


def test_the_downgrade_refuses_over_a_top_up_and_round_trips_without_one(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    config = make_alembic_config(disposable_database)
    command.upgrade(config, REVISION)
    execute(disposable_database, SEED)
    execute(disposable_database, TOP_UP)

    with pytest.raises(RuntimeError, match="holds level_top_up lots"):
        command.downgrade(config, PREVIOUS)
    # Refused before touching anything: the lot and the wide check both remain.
    assert scalar(disposable_database, "SELECT count(*) FROM credit_lots") == 1
    assert "level_top_up" in scalar(disposable_database, DEFINITION)

    execute(disposable_database, "DELETE FROM credit_lots")
    command.downgrade(config, PREVIOUS)
    assert "level_top_up" not in scalar(disposable_database, DEFINITION)
    with pytest.raises(Exception, match="ck_credit_lots_source_is_known"):
        execute(disposable_database, TOP_UP)

    command.upgrade(config, REVISION)
    execute(disposable_database, TOP_UP)
    assert scalar(disposable_database, "SELECT count(*) FROM credit_lots") == 1
