"""What `e5b1c7d3a920` leaves behind, beyond "the chain runs".

Split from `test_migrations.py` by area, as the file-size limit asks
(non-negotiable 11); the helpers are imported from there, not copied.
"""

import pytest
from alembic import command
from tests.integration.test_migrations import ConfigFactory, execute, scalar

pytestmark = pytest.mark.db

REVISION = "e5b1c7d3a920"
PREVIOUS = "b7779986392d"

TABLE = "SELECT to_regclass('public.session_room_stays') IS NOT NULL"

#: One session with one participant, built in SQL: the app's factories need head.
SEED = """
INSERT INTO users (id, auth_id, email, primary_role, timezone) VALUES
  ('00000000-0000-7000-8000-000000000001', gen_random_uuid(),
   'm@example.test', 'mentor', 'Africa/Lagos'),
  ('00000000-0000-7000-8000-000000000002', gen_random_uuid(),
   'e@example.test', 'mentee', 'Africa/Lagos');
INSERT INTO mentor_profiles (user_id) VALUES ('00000000-0000-7000-8000-000000000001');
INSERT INTO sessions (id, mentor_id, mentee_id, starts_at, duration_minutes, status)
VALUES ('00000000-0000-7000-8000-000000000003',
        '00000000-0000-7000-8000-000000000001',
        '00000000-0000-7000-8000-000000000002', now(), 30, 'confirmed');
INSERT INTO session_participants (id, session_id, user_id, role)
VALUES ('00000000-0000-7000-8000-000000000004',
        '00000000-0000-7000-8000-000000000003',
        '00000000-0000-7000-8000-000000000002', 'mentee');
"""

STAY = (
    "INSERT INTO session_room_stays (participant_id, provider_session_id, joined_at, left_at) "
    "VALUES ('00000000-0000-7000-8000-000000000004', {})"
)


def test_the_table_carries_its_invariants(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """A generated id, one row per join, a leave never before its join, a
    bounded provider id, and rows that go with their participant."""
    config = make_alembic_config(disposable_database)
    command.upgrade(config, REVISION)
    execute(disposable_database, SEED)

    execute(disposable_database, STAY.format("'join-1', now(), NULL"))
    # A second join by the same party is a second row.
    execute(disposable_database, STAY.format("'join-2', now(), now() + interval '1 minute'"))

    assert scalar(disposable_database, "SELECT count(*) FROM session_room_stays") == 2
    with pytest.raises(Exception, match="uq_session_room_stays_join"):
        execute(disposable_database, STAY.format("'join-1', now(), NULL"))
    with pytest.raises(Exception, match="left_after_joined"):
        execute(disposable_database, STAY.format("'join-3', now(), now() - interval '1 second'"))
    with pytest.raises(Exception, match="provider_session_id_length"):
        execute(disposable_database, STAY.format("'', now(), NULL"))
    with pytest.raises(Exception, match="provider_session_id_length"):
        execute(disposable_database, STAY.format(f"'{'x' * 129}', now(), NULL"))
    # The bounds themselves are allowed: one character, 128 characters, a leave
    # at the instant of the join.
    execute(disposable_database, STAY.format("'y', now(), NULL"))
    execute(disposable_database, STAY.format(f"'{'z' * 128}', now(), now()"))
    assert scalar(
        disposable_database,
        "SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_set_updated_at' "
        "AND tgrelid = 'session_room_stays'::regclass",
    )

    execute(disposable_database, "DELETE FROM session_participants")

    assert scalar(disposable_database, "SELECT count(*) FROM session_room_stays") == 0


def test_the_downgrade_removes_the_table(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """Live state: dropping it loses nothing attendance relies on, so the
    downgrade does not refuse while it holds rows."""
    config = make_alembic_config(disposable_database)
    command.upgrade(config, REVISION)
    execute(disposable_database, SEED)
    execute(disposable_database, STAY.format("'join-1', now(), NULL"))

    command.downgrade(config, PREVIOUS)

    assert scalar(disposable_database, TABLE) is False
