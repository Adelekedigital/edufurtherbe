"""What `9d3b7f1a2c64` leaves behind, beyond "the chain runs".

Split from `test_migrations.py` by area, as the file-size limit asks
(non-negotiable 11); the helpers are imported from there, not copied.
"""

import pytest
from alembic import command
from tests.integration.test_migrations import ConfigFactory, execute, scalar

from app.domain.admin_notes import NOTE_BODY_MAX_LENGTH

pytestmark = pytest.mark.db

REVISION = "9d3b7f1a2c64"
PREVIOUS = "8489be263031"

TABLE = "SELECT to_regclass('public.mentor_admin_notes') IS NOT NULL"

MENTOR = "00000000-0000-7000-8000-000000000001"
ADMIN = "00000000-0000-7000-8000-000000000002"

#: A mentor and an admin, built in SQL: the app's factories need head.
SEED = """
INSERT INTO users (id, auth_id, email, primary_role, timezone) VALUES
  ('00000000-0000-7000-8000-000000000001', gen_random_uuid(), 'm@example.test',
   'mentor', 'Africa/Lagos'),
  ('00000000-0000-7000-8000-000000000002', gen_random_uuid(), 'a@example.test',
   'mentee', 'Africa/Lagos');
INSERT INTO mentor_profiles (user_id) VALUES ('00000000-0000-7000-8000-000000000001');
"""


NOTE = "INSERT INTO mentor_admin_notes (mentor_user_id, author_id, body) VALUES ('{}', '{}', '{}')"

DELETE_ADMIN = "DELETE FROM users WHERE id = '00000000-0000-7000-8000-000000000002'"


def note(body: str, mentor: str = MENTOR, author: str = ADMIN) -> str:
    return NOTE.format(mentor, author, body)


def test_the_table_carries_its_invariants(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """A generated id and time, a bounded non-empty body, an author who cannot
    be deleted out from under a note, and notes that go with their profile."""
    config = make_alembic_config(disposable_database)
    command.upgrade(config, REVISION)
    execute(disposable_database, SEED)

    execute(disposable_database, note("x" * NOTE_BODY_MAX_LENGTH))
    execute(disposable_database, note("Second look"))

    assert (
        scalar(
            disposable_database,
            "SELECT count(*) FROM mentor_admin_notes WHERE id IS NOT NULL "
            "AND created_at IS NOT NULL",
        )
        == 2
    )
    with pytest.raises(Exception, match="ck_mentor_admin_notes_body_length"):
        execute(disposable_database, note(""))
    with pytest.raises(Exception, match="ck_mentor_admin_notes_body_length"):
        execute(disposable_database, note("x" * (NOTE_BODY_MAX_LENGTH + 1)))
    with pytest.raises(Exception, match="fk_mentor_admin_notes_mentor_user_id_mentor_profiles"):
        # The admin has no mentor profile, so a note cannot be about them.
        execute(disposable_database, note("About nobody", mentor=ADMIN))
    with pytest.raises(Exception, match="fk_mentor_admin_notes_author_id_users"):
        execute(disposable_database, DELETE_ADMIN)

    execute(disposable_database, "DELETE FROM mentor_profiles")

    assert scalar(disposable_database, "SELECT count(*) FROM mentor_admin_notes") == 0


def test_the_constraint_names_the_bound_the_code_reads(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """The migration spells the bound out; this pins it to the constant the API
    and the model read, so the two cannot drift."""
    config = make_alembic_config(disposable_database)
    command.upgrade(config, REVISION)

    body = scalar(
        disposable_database,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'ck_mentor_admin_notes_body_length'",
    )
    index = scalar(
        disposable_database,
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_mentor_admin_notes_mentor'",
    )

    assert body == (
        f"CHECK (((char_length(body) >= 1) AND (char_length(body) <= {NOTE_BODY_MAX_LENGTH})))"
    )
    assert index.endswith("(mentor_user_id, created_at)")


def test_the_downgrade_removes_the_table(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    config = make_alembic_config(disposable_database)
    command.upgrade(config, REVISION)

    command.downgrade(config, PREVIOUS)

    assert scalar(disposable_database, TABLE) is False
    command.upgrade(config, REVISION)
    assert scalar(disposable_database, TABLE) is True


INDEX_REVISION = "4e8b2c6d1f93"
LOG_INDEX = "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_credit_transactions_created'"


def test_the_credit_log_index_orders_the_whole_ledger_newest_first(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """`4e8b2c6d1f93` (same PR): the admin credit log's keyset order, and a
    downgrade that removes it."""
    config = make_alembic_config(disposable_database)
    command.upgrade(config, INDEX_REVISION)

    index = scalar(disposable_database, LOG_INDEX)
    assert index.endswith("ON public.credit_transactions USING btree (created_at DESC, id DESC)")

    command.downgrade(config, REVISION)
    assert scalar(disposable_database, LOG_INDEX) is None
