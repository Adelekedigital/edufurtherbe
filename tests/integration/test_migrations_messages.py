"""What `5c2e8a4f1b37` leaves behind, beyond "the chain runs" (ADR 0010).

Split from `test_migrations.py` by area, as the file-size limit asks
(non-negotiable 11); the helpers are imported from there, not copied.
"""

import pytest
from alembic import command
from tests.integration.test_migrations import ConfigFactory, execute, scalar

from app.domain.chats import BODY_MAX_LENGTH

pytestmark = pytest.mark.db

REVISION = "5c2e8a4f1b37"
PREVIOUS = "4e8b2c6d1f93"

MENTOR = "00000000-0000-7000-8000-000000000001"
MENTEE = "00000000-0000-7000-8000-000000000002"
OTHER = "00000000-0000-7000-8000-000000000003"
CHAT = "00000000-0000-7000-8000-0000000000c1"
CLIENT = "00000000-0000-7000-8000-0000000000d1"

#: Three people and one chat, built in SQL: the app's factories need head.
SEED_TEMPLATE = """
INSERT INTO users (id, auth_id, email, primary_role, timezone) VALUES
  ('{mentor}', gen_random_uuid(), 'm@example.test', 'mentor', 'UTC'),
  ('{mentee}', gen_random_uuid(), 'e@example.test', 'mentee', 'UTC'),
  ('{other}', gen_random_uuid(), 'o@example.test', 'mentee', 'UTC');
INSERT INTO conversations (id, status, mentor_user_id, mentee_user_id, created_by)
VALUES ('{chat}', 'accepted', '{mentor}', '{mentee}', '{mentee}');
"""
SEED = SEED_TEMPLATE.format(mentor=MENTOR, mentee=MENTEE, other=OTHER, chat=CHAT)

CHAT_ROW = (
    "INSERT INTO conversations (status, mentor_user_id, mentee_user_id) VALUES ('{}', '{}', '{}')"
)
MESSAGE_ROW = (
    "INSERT INTO messages (conversation_id, sender_id, message_type, body, client_id) "
    "VALUES ('{}', '{}', '{}', {}, {})"
)
ATTACHMENT_ROW = (
    "INSERT INTO message_attachments (message_id, uploaded_by, storage_key, file_name, "
    "mime_type, size_bytes) VALUES ({}, '{}', '{}', {}, {}, {})"
)
PARTICIPANT_ROW = (
    "INSERT INTO conversation_participants (conversation_id, user_id) VALUES ('{}', '{}')"
)
DELETE_USER = "DELETE FROM users WHERE id = '{}'"
IMPORTED = (
    "INSERT INTO messages (conversation_id, sender_id, message_type, body, legacy_bubble_id) "
    "VALUES ('{}', '{}', 'text', {}, gen_random_uuid()::text)"
)
COUNT = "SELECT count(*) FROM {}"
AGE = "UPDATE {} SET updated_at = now() - interval '1 day'"
TOUCH = "UPDATE {} SET created_at = created_at"
MOVED = "SELECT bool_and(updated_at > now() - interval '1 hour') FROM {}"

TABLES = ("conversations", "conversation_participants", "messages", "message_attachments")


def chat(mentor: str, mentee: str, status: str = "accepted") -> str:
    return CHAT_ROW.format(status, mentor, mentee)


def message(
    body: str | None = "'Hello'",
    *,
    kind: str = "text",
    sender: str = MENTEE,
    client: str | None = f"'{CLIENT}'",
) -> str:
    return MESSAGE_ROW.format(CHAT, sender, kind, body or "NULL", client or "NULL")


def attachment(
    message_id: str = "NULL", *, key: str = "k1", uploader: str = MENTEE, **overrides: str
) -> str:
    values = {
        "file_name": "'cv.pdf'",
        "mime_type": "'application/pdf'",
        "size_bytes": "10",
        **overrides,
    }
    return ATTACHMENT_ROW.format(
        message_id, uploader, key, values["file_name"], values["mime_type"], values["size_bytes"]
    )


def upgraded(url: str, config_factory: ConfigFactory) -> None:
    command.upgrade(config_factory(url), REVISION)
    execute(url, SEED)


def test_two_people_have_one_chat_whichever_is_the_mentor(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """The pair is unordered: the same two people reversed are refused, and
    nobody chats with themselves. Another pair is free."""
    upgraded(disposable_database, make_alembic_config)

    with pytest.raises(Exception, match="uq_conversations_pair"):
        execute(disposable_database, chat(MENTOR, MENTEE))
    with pytest.raises(Exception, match="uq_conversations_pair"):
        execute(disposable_database, chat(MENTEE, MENTOR))
    with pytest.raises(Exception, match="ck_conversations_two_people"):
        execute(disposable_database, chat(OTHER, OTHER))
    with pytest.raises(Exception, match="ck_conversations_status_is_known"):
        execute(disposable_database, chat(MENTOR, OTHER, status="blocked"))
    execute(disposable_database, chat(MENTOR, OTHER, status="pending"))

    assert scalar(disposable_database, "SELECT count(*) FROM conversations") == 2


def test_a_client_id_lands_once_per_sender(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """A retried send is refused by `(sender_id, client_id)`; the other person
    may reuse the value, and imported messages carry none."""
    upgraded(disposable_database, make_alembic_config)
    execute(disposable_database, message())

    with pytest.raises(Exception, match="uq_messages_sender_client"):
        execute(disposable_database, message("'Again'"))
    execute(disposable_database, message(sender=MENTOR))
    execute(disposable_database, message(client=None))
    execute(disposable_database, message(client=None))

    assert scalar(disposable_database, "SELECT count(*) FROM messages") == 4
    assert (
        scalar(
            disposable_database,
            "SELECT count(*) FROM messages WHERE id IS NOT NULL AND sent_at IS NOT NULL",
        )
        == 4
    )


def test_a_message_body_is_bounded_and_text_has_one(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    upgraded(disposable_database, make_alembic_config)

    execute(disposable_database, message(f"repeat('x', {BODY_MAX_LENGTH})"))
    with pytest.raises(Exception, match="ck_messages_body_length"):
        execute(disposable_database, message(f"repeat('x', {BODY_MAX_LENGTH + 1})", client=None))
    with pytest.raises(Exception, match="ck_messages_body_length"):
        execute(disposable_database, message("''", client=None))
    with pytest.raises(Exception, match="ck_messages_text_has_body"):
        execute(disposable_database, message(None, client=None))
    with pytest.raises(Exception, match="ck_messages_message_type_is_known"):
        execute(disposable_database, message(kind="system", client=None))
    # A file alone has no text.
    execute(disposable_database, message(None, kind="attachment", client=None))
    # An imported message carries what Bubble held: no bound, possibly blank.
    for body in (f"repeat('x', {BODY_MAX_LENGTH + 1})", "''", None):
        execute(disposable_database, IMPORTED.format(CHAT, MENTEE, body or "NULL"))

    assert scalar(disposable_database, "SELECT count(*) FROM messages") == 5


def test_an_attachment_waits_unsent_and_a_message_holds_one(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    upgraded(disposable_database, make_alembic_config)
    execute(disposable_database, message(None, kind="attachment"))
    sent = scalar(disposable_database, "SELECT id FROM messages")

    execute(disposable_database, attachment(key="k1"))
    execute(disposable_database, attachment(key="k2"))
    execute(disposable_database, attachment(f"'{sent}'", key="k3"))

    with pytest.raises(Exception, match="uq_message_attachments_message_id"):
        execute(disposable_database, attachment(f"'{sent}'", key="k4"))
    with pytest.raises(Exception, match="uq_message_attachments_storage_key"):
        execute(disposable_database, attachment(key="k1"))
    with pytest.raises(Exception, match="ck_message_attachments_mime_type_is_known"):
        execute(disposable_database, attachment(key="k5", mime_type="'application/zip'"))
    with pytest.raises(Exception, match="ck_message_attachments_size_is_positive"):
        execute(disposable_database, attachment(key="k6", size_bytes="0"))
    with pytest.raises(Exception, match="ck_message_attachments_file_name_length"):
        execute(disposable_database, attachment(key="k7", file_name="''"))

    assert (
        scalar(
            disposable_database,
            "SELECT string_agg(DISTINCT virus_scan_status, ',') FROM message_attachments",
        )
        == "skipped"
    )
    with pytest.raises(Exception, match="ck_message_attachments_virus_scan_status_is_known"):
        execute(disposable_database, "UPDATE message_attachments SET virus_scan_status = 'clean'")


def test_a_chat_takes_its_rows_with_it_and_people_cannot_be_deleted_out_from_under_it(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """Every child of a chat cascades from it; a sender, a pair member and an
    uploader restrict."""
    upgraded(disposable_database, make_alembic_config)
    execute(
        disposable_database,
        PARTICIPANT_ROW.format(CHAT, MENTOR),
    )
    execute(
        disposable_database,
        PARTICIPANT_ROW.format(CHAT, MENTEE),
    )
    with pytest.raises(Exception, match="uq_conversation_participants_conversation_user"):
        execute(disposable_database, PARTICIPANT_ROW.format(CHAT, MENTOR))
    # The schema does not know who is in the chat (the write path does), so a
    # sender outside the pair shows the sender's key on its own.
    execute(disposable_database, message(sender=OTHER))
    sent = scalar(disposable_database, "SELECT id FROM messages")
    execute(disposable_database, attachment(f"'{sent}'"))

    with pytest.raises(Exception, match="fk_messages_sender_id_users"):
        execute(disposable_database, DELETE_USER.format(OTHER))
    execute(disposable_database, "DELETE FROM messages")
    assert scalar(disposable_database, "SELECT count(*) FROM message_attachments") == 0
    execute(disposable_database, attachment(key="unsent", uploader=OTHER))
    with pytest.raises(Exception, match="fk_message_attachments_uploaded_by_users"):
        execute(disposable_database, DELETE_USER.format(OTHER))
    with pytest.raises(Exception, match="fk_conversations_mentor_user_id_users"):
        execute(disposable_database, DELETE_USER.format(MENTOR))

    execute(disposable_database, message(sender=MENTEE))
    execute(disposable_database, attachment("(SELECT id FROM messages)", key="k2"))
    execute(disposable_database, "DELETE FROM conversations")

    for table in TABLES[:3]:
        assert scalar(disposable_database, COUNT.format(table)) == 0
    # Only the unsent upload is left: it belongs to no chat yet.
    assert scalar(disposable_database, "SELECT storage_key FROM message_attachments") == "unsent"


def test_the_constraints_name_the_bounds_the_code_reads(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """The migration spells the bound and the pair out; this pins them to the
    constant the API reads and to the unordered pair ADR 0010 promises."""
    upgraded(disposable_database, make_alembic_config)

    body = scalar(
        disposable_database,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'ck_messages_body_length'",
    )
    pair = scalar(
        disposable_database,
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_conversations_pair'",
    )
    thread = scalar(
        disposable_database,
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_messages_conversation_sent'",
    )

    assert body == (
        "CHECK (((legacy_bubble_id IS NOT NULL) OR ((char_length(body) >= 1) AND "
        f"(char_length(body) <= {BODY_MAX_LENGTH}))))"
    )
    assert pair.startswith("CREATE UNIQUE INDEX")
    assert pair.endswith(
        "(LEAST(mentor_user_id, mentee_user_id), GREATEST(mentor_user_id, mentee_user_id))"
    )
    assert thread.endswith("(conversation_id, sent_at, id)")


def test_updated_at_moves_on_every_table(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """Each table has the trigger, so an `UPDATE` drags `updated_at` forward."""
    upgraded(disposable_database, make_alembic_config)
    execute(
        disposable_database,
        PARTICIPANT_ROW.format(CHAT, MENTEE),
    )
    execute(disposable_database, message())
    execute(disposable_database, attachment())

    for table in TABLES:
        execute(
            disposable_database,
            AGE.format(table),
        )
        execute(disposable_database, TOUCH.format(table))
        assert (
            scalar(
                disposable_database,
                MOVED.format(table),
            )
            is True
        ), table


def test_the_downgrade_removes_the_tables_and_refuses_over_rows(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    config = make_alembic_config(disposable_database)
    upgraded(disposable_database, make_alembic_config)

    with pytest.raises(RuntimeError, match="conversations is not empty"):
        command.downgrade(config, PREVIOUS)

    execute(disposable_database, "DELETE FROM conversations")
    command.downgrade(config, PREVIOUS)

    for table in TABLES:
        assert scalar(disposable_database, f"SELECT to_regclass('public.{table}')") is None
    command.upgrade(config, REVISION)
    assert scalar(disposable_database, "SELECT to_regclass('public.messages')") is not None
