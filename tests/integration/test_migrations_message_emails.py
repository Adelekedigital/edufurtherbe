"""What `a7d3e5c91b42` leaves behind (decision 303): `messages.emailed_at` and
`notification_channel_preferences`.

Split from `test_migrations.py` by area, as the file-size limit asks
(non-negotiable 11); the helpers are imported from there, not copied.
"""

import pytest
from alembic import command
from tests.integration.test_migrations import ConfigFactory, execute, scalar

from app.domain.enums import NotificationPreferenceType

pytestmark = pytest.mark.db

REVISION = "a7d3e5c91b42"
PREVIOUS = "5c2e8a4f1b37"

MENTOR = "00000000-0000-7000-8000-000000000001"
MENTEE = "00000000-0000-7000-8000-000000000002"
OTHER = "00000000-0000-7000-8000-000000000003"
CHAT = "00000000-0000-7000-8000-0000000000c1"

SEED_TEMPLATE = """
INSERT INTO users (id, auth_id, email, primary_role, timezone) VALUES
  ('{MENTOR}', gen_random_uuid(), 'm@example.test', 'mentor', 'UTC'),
  ('{MENTEE}', gen_random_uuid(), 'e@example.test', 'mentee', 'UTC'),
  ('{OTHER}', gen_random_uuid(), 'o@example.test', 'mentee', 'UTC');
INSERT INTO conversations (id, status, mentor_user_id, mentee_user_id, created_by)
VALUES ('{CHAT}', 'accepted', '{MENTOR}', '{MENTEE}', '{MENTEE}');
INSERT INTO messages (conversation_id, sender_id, message_type, body)
VALUES ('{CHAT}', '{MENTEE}', 'text', 'Hello');
"""
SEED = SEED_TEMPLATE.format(MENTOR=MENTOR, MENTEE=MENTEE, OTHER=OTHER, CHAT=CHAT)

PREFERENCE = (
    "INSERT INTO notification_channel_preferences (user_id, notification_type, email) "
    "VALUES ('{}', '{}', false)"
)
TABLE = "notification_channel_preferences"
BARE = (
    "INSERT INTO notification_channel_preferences (user_id, notification_type) VALUES ('{}', '{}')"
)
BARE_DEFAULTS = (
    "SELECT in_app AND email AND NOT push AND NOT whatsapp "
    "FROM notification_channel_preferences WHERE user_id = '{}'"
)
UNREAD = NotificationPreferenceType.UNREAD_MESSAGES.value
COUNT = "SELECT count(*) FROM notification_channel_preferences"
DEFAULTS = (
    "SELECT bool_and(in_app AND NOT email AND NOT push AND NOT whatsapp AND id IS NOT NULL) "
    "FROM notification_channel_preferences"
)
AGE = "UPDATE notification_channel_preferences SET updated_at = now() - interval '1 day'"
TOUCH = "UPDATE notification_channel_preferences SET email = true"
MOVED = "SELECT updated_at > now() - interval '1 hour' FROM notification_channel_preferences"
EXISTS = "SELECT to_regclass('public.notification_channel_preferences')"
DELETE_USER = "DELETE FROM users WHERE id = '{}'"
EMPTIED = "DELETE FROM notification_channel_preferences"


def upgraded(url: str, config_factory: ConfigFactory) -> None:
    command.upgrade(config_factory(url), REVISION)
    execute(url, SEED)


def test_a_preference_is_one_row_per_person_and_email(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """The package's composite key survives as a unique pair; the vocabulary is
    checked; the other channels keep the package's defaults."""
    upgraded(disposable_database, make_alembic_config)
    execute(disposable_database, PREFERENCE.format(MENTEE, UNREAD))
    execute(disposable_database, PREFERENCE.format(MENTOR, UNREAD))
    # Every channel unsaid: the package's defaults, email on.
    execute(disposable_database, BARE.format(OTHER, UNREAD))
    assert scalar(disposable_database, BARE_DEFAULTS.format(OTHER)) is True
    execute(disposable_database, DELETE_USER.format(OTHER))

    with pytest.raises(Exception, match="uq_notification_channel_preferences_user_type"):
        execute(disposable_database, PREFERENCE.format(MENTEE, UNREAD))
    with pytest.raises(
        Exception, match="ck_notification_channel_preferences_notification_type_is_known"
    ):
        execute(disposable_database, PREFERENCE.format(MENTEE, "marketing"))

    assert scalar(disposable_database, COUNT) == 2
    assert scalar(disposable_database, DEFAULTS) is True


def test_a_preference_goes_with_its_person_and_its_updated_at_moves(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    upgraded(disposable_database, make_alembic_config)
    execute(disposable_database, PREFERENCE.format(MENTOR, UNREAD))
    execute(disposable_database, AGE)
    execute(disposable_database, TOUCH)
    assert scalar(disposable_database, MOVED) is True

    # Nothing else holds the mentor once their chat is gone.
    execute(disposable_database, "DELETE FROM conversations")
    execute(disposable_database, DELETE_USER.format(MENTOR))
    assert scalar(disposable_database, COUNT) == 0


def test_emailed_at_is_optional_and_its_index_covers_only_emailed_messages(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    upgraded(disposable_database, make_alembic_config)
    assert scalar(disposable_database, "SELECT emailed_at FROM messages") is None
    execute(disposable_database, "UPDATE messages SET emailed_at = now()")

    index = scalar(
        disposable_database,
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_messages_conversation_emailed'",
    )
    assert index.endswith("(conversation_id, emailed_at) WHERE (emailed_at IS NOT NULL)")


def test_the_downgrade_refuses_over_a_preference_or_an_emailed_message(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    config = make_alembic_config(disposable_database)
    upgraded(disposable_database, make_alembic_config)
    execute(disposable_database, PREFERENCE.format(MENTEE, UNREAD))
    execute(disposable_database, "UPDATE messages SET emailed_at = now()")

    with pytest.raises(RuntimeError, match=f"{TABLE} is not empty"):
        command.downgrade(config, PREVIOUS)
    execute(disposable_database, EMPTIED)
    with pytest.raises(RuntimeError, match="emailed_at is set"):
        command.downgrade(config, PREVIOUS)
    execute(disposable_database, "UPDATE messages SET emailed_at = NULL")
    command.downgrade(config, PREVIOUS)

    assert scalar(disposable_database, EXISTS) is None
    assert (
        scalar(
            disposable_database,
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'messages' AND column_name = 'emailed_at'",
        )
        == 0
    )
    command.upgrade(config, REVISION)
    assert scalar(disposable_database, EXISTS) is not None
