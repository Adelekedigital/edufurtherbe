"""What `d4a9e2c7b5f1` leaves behind, beyond "the chain runs".

Split from `test_migrations.py` by area, as the file-size limit asks
(non-negotiable 11); the helpers are imported from there, not copied.
"""

import pytest
from alembic import command
from tests.integration.test_migrations import ConfigFactory, execute, scalar

from app.domain.enums import MissedReason
from app.domain.missed_reasons import NOTE_MAX_LENGTH

pytestmark = pytest.mark.db

REVISION = "d4a9e2c7b5f1"
PREVIOUS = "6f3f4bbaf321"

TABLE = "SELECT to_regclass('public.session_missed_reasons') IS NOT NULL"

MENTOR = "00000000-0000-7000-8000-000000000001"
MENTEE = "00000000-0000-7000-8000-000000000002"
SESSION = "00000000-0000-7000-8000-000000000003"

#: Two people and one missed session, built in SQL: the app's factories need head.
SEED = """
INSERT INTO users (id, auth_id, email, primary_role, timezone) VALUES
  ('00000000-0000-7000-8000-000000000001', gen_random_uuid(), 'm@example.test',
   'mentor', 'Africa/Lagos'),
  ('00000000-0000-7000-8000-000000000002', gen_random_uuid(), 'e@example.test',
   'mentee', 'Africa/Lagos');
INSERT INTO sessions (id, mentor_id, mentee_id, starts_at, duration_minutes, status)
VALUES ('00000000-0000-7000-8000-000000000003', '00000000-0000-7000-8000-000000000001',
        '00000000-0000-7000-8000-000000000002', now() - interval '1 day', 45, 'no_show');
"""


ROW = (
    "INSERT INTO session_missed_reasons (session_id, user_id, reason, note) "
    "VALUES ('" + SESSION + "', '{}', '{}', {})"
)

CLEAR = "DELETE FROM session_missed_reasons"


def row(user: str, reason: str = "forgot", note: str = "NULL") -> str:
    return ROW.format(user, reason, note)


def test_the_table_carries_its_invariants(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """A generated id, one reason per party per session, a known reason, a
    bounded note, and rows that go with their session."""
    config = make_alembic_config(disposable_database)
    command.upgrade(config, REVISION)
    execute(disposable_database, SEED)

    execute(disposable_database, row(MENTEE, note=f"'{'x' * NOTE_MAX_LENGTH}'"))
    # Both parties may send: the pair, not the session, is unique.
    execute(disposable_database, row(MENTOR, reason="wrong_time_zone"))

    assert scalar(disposable_database, "SELECT count(*) FROM session_missed_reasons") == 2
    assert (
        scalar(
            disposable_database,
            "SELECT count(*) FROM session_missed_reasons WHERE id IS NOT NULL "
            "AND created_at IS NOT NULL",
        )
        == 2
    )
    with pytest.raises(Exception, match="uq_session_missed_reasons_session_user"):
        execute(disposable_database, row(MENTEE, reason="other"))
    with pytest.raises(Exception, match="ck_session_missed_reasons_reason_is_known"):
        execute(disposable_database, CLEAR)
        execute(disposable_database, row(MENTEE, "late"))
    with pytest.raises(Exception, match="ck_session_missed_reasons_note_length"):
        execute(disposable_database, row(MENTEE, note=f"'{'x' * (NOTE_MAX_LENGTH + 1)}'"))

    execute(disposable_database, "DELETE FROM sessions")

    assert scalar(disposable_database, "SELECT count(*) FROM session_missed_reasons") == 0


def test_the_constraints_name_what_the_code_does(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """The migration spells its values out; these pin them to the enum and the
    bound the API and the model read, so the two cannot drift."""
    config = make_alembic_config(disposable_database)
    command.upgrade(config, REVISION)

    reason = scalar(
        disposable_database,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'ck_session_missed_reasons_reason_is_known'",
    )
    note = scalar(
        disposable_database,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'ck_session_missed_reasons_note_length'",
    )

    assert all(f"'{member.value}'" in reason for member in MissedReason)
    assert reason.count("'") == 2 * len(MissedReason)
    assert note == f"CHECK ((char_length(note) <= {NOTE_MAX_LENGTH}))"


def test_the_downgrade_removes_the_table(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    config = make_alembic_config(disposable_database)
    command.upgrade(config, REVISION)

    command.downgrade(config, PREVIOUS)

    assert scalar(disposable_database, TABLE) is False
    command.upgrade(config, REVISION)
    assert scalar(disposable_database, TABLE) is True
