"""What `b7779986392d` leaves behind, beyond "the chain runs".

Split from `test_migrations.py` by area, as the file-size limit asks
(non-negotiable 11); the helpers are imported from there, not copied.
"""

import pytest
from alembic import command
from tests.integration.test_migrations import ConfigFactory, execute, scalar

pytestmark = pytest.mark.db

REVISION = "b7779986392d"
PREVIOUS = "c3e8a1f5d29b"

TABLE = "SELECT to_regclass('public.session_type_next_availability') IS NOT NULL"

#: One mentor with one offering, built in SQL: the app's factories need head.
SEED = """
INSERT INTO users (id, auth_id, email, primary_role, timezone)
VALUES ('00000000-0000-7000-8000-000000000001', gen_random_uuid(),
        'm@example.test', 'mentor', 'Africa/Lagos');
INSERT INTO mentor_profiles (user_id)
VALUES ('00000000-0000-7000-8000-000000000001');
INSERT INTO session_types (id, mentor_user_id, name)
VALUES ('00000000-0000-7000-8000-000000000002',
        '00000000-0000-7000-8000-000000000001', 'Chat');
"""

ROW = (
    "INSERT INTO session_type_next_availability "
    "(mentor_user_id, session_type_id, next_available_at, bookable_until) VALUES "
    "('00000000-0000-7000-8000-000000000001', '00000000-0000-7000-8000-000000000002', {})"
)


def test_the_table_carries_its_invariants(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """A generated id, one row per (mentor, offering), a deadline exactly when
    there is a time, and rows that go with their offering."""
    config = make_alembic_config(disposable_database)
    command.upgrade(config, REVISION)
    execute(disposable_database, SEED)

    execute(disposable_database, ROW.format("now() + interval '1 day', now()"))

    assert scalar(disposable_database, "SELECT count(*) FROM session_type_next_availability") == 1
    assert (
        scalar(
            disposable_database,
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_session_type_next_availability_deadline_with_time'",
        )
        == "CHECK (((next_available_at IS NULL) = (bookable_until IS NULL)))"
    )
    with pytest.raises(Exception, match="uq_session_type_next_availability_pair"):
        execute(disposable_database, ROW.format("NULL, NULL"))
    # One row per offering whatever mentor id a writer names.
    execute(
        disposable_database,
        "INSERT INTO users (id, auth_id, email, primary_role, timezone) VALUES "
        "('00000000-0000-7000-8000-000000000003', gen_random_uuid(), 'o@example.test', "
        "'mentor', 'Africa/Lagos'); "
        "INSERT INTO mentor_profiles (user_id) VALUES ('00000000-0000-7000-8000-000000000003')",
    )
    with pytest.raises(Exception, match="uq_session_type_next_availability_session_type"):
        execute(
            disposable_database,
            ROW.format("NULL, NULL").replace(
                "'00000000-0000-7000-8000-000000000001'", "'00000000-0000-7000-8000-000000000003'"
            ),
        )
    with pytest.raises(Exception, match="deadline_with_time"):
        execute(
            disposable_database,
            "UPDATE session_type_next_availability SET bookable_until = NULL",
        )

    execute(disposable_database, "DELETE FROM session_types")

    assert scalar(disposable_database, "SELECT count(*) FROM session_type_next_availability") == 0


def test_the_downgrade_removes_the_table(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """A cache: dropping it loses nothing the next refresh cannot rebuild, so
    the downgrade does not refuse while it holds rows."""
    config = make_alembic_config(disposable_database)
    command.upgrade(config, REVISION)
    execute(disposable_database, SEED)
    execute(disposable_database, ROW.format("NULL, NULL"))

    command.downgrade(config, PREVIOUS)

    assert scalar(disposable_database, TABLE) is False
    command.upgrade(config, REVISION)
    assert scalar(disposable_database, TABLE) is True
