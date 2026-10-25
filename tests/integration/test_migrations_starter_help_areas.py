"""What `056f597d551a` leaves behind: the starter backfill, its index, and the
help-area answer form, beyond "the chain runs".

Split from `test_migrations.py` by area (non-negotiable 11); the helpers are
imported from there, not copied.
"""

import pytest
from alembic import command
from tests.integration.test_migrations import (
    MENTOR_WITH_OFFERING,
    ConfigFactory,
    execute,
    scalar,
)

pytestmark = pytest.mark.db

REVISION = "056f597d551a"
PREVIOUS = "b8d2e6f4a3c7"


def _offering(url: str, email: str, name: str) -> None:
    execute(url, MENTOR_WITH_OFFERING.format(email=email, name=name, stage="NULL", label="NULL"))


#: Every starter, as `email/name`.
STARTERS = (
    "SELECT coalesce(string_agg(u.email || '/' || t.name, ',' ORDER BY u.email), '') "
    "FROM session_types t JOIN users u ON u.id = t.mentor_user_id WHERE t.is_starter"
)


def test_the_backfill_marks_only_a_live_intro_chat(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """Onboarding's offering is a live one named exactly `Intro chat`; another
    name, another casing or a deleted one is not the starter."""
    url = disposable_database
    config = make_alembic_config(url)
    command.upgrade(config, PREVIOUS)
    _offering(url, "onboarded@example.test", "Intro chat")
    _offering(url, "lower@example.test", "intro chat")
    _offering(url, "other@example.test", "SOP review")
    _offering(url, "deleted@example.test", "Intro chat")
    execute(
        url,
        "UPDATE session_types SET deleted_at = now() WHERE mentor_user_id = "
        "(SELECT id FROM users WHERE email = 'deleted@example.test')",
    )

    command.upgrade(config, REVISION)

    assert scalar(url, STARTERS) == "onboarded@example.test/Intro chat"


OWNED_BY_TWO = "mentor_user_id = (SELECT id FROM users WHERE email = 'two@example.test')"
SECOND_STARTER = (
    "INSERT INTO session_types (mentor_user_id, name, is_starter) "
    "SELECT id, 'Second', true FROM users WHERE email = 'two@example.test'"
)


def test_a_mentor_holds_one_live_starter(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    url = disposable_database
    command.upgrade(make_alembic_config(url), REVISION)
    _offering(url, "two@example.test", "Intro chat")
    execute(url, f"UPDATE session_types SET is_starter = true WHERE {OWNED_BY_TWO}")  # noqa: S608

    with pytest.raises(Exception, match="ix_session_types_one_starter"):
        execute(url, SECOND_STARTER)
    # Deleting the starter frees the slot.
    execute(url, f"UPDATE session_types SET deleted_at = now() WHERE {OWNED_BY_TWO}")  # noqa: S608
    execute(url, SECOND_STARTER)


#: An answer naming a help area, its submission and question faked: replica
#: mode skips foreign keys (never `CHECK`s), so the downgrade guard can be met
#: without building a whole booking.
HELP_AREA_ANSWER = """
    SET session_replication_role = replica;
    INSERT INTO intake_answers (submission_id, question_id, selected_offering_id, option_text)
    VALUES (gen_random_uuid(), gen_random_uuid(),
            (SELECT id FROM service_offerings ORDER BY sort_order LIMIT 1), 'Essays');
    SET session_replication_role = DEFAULT;
"""


def test_the_downgrade_refuses_while_an_answer_holds_a_help_area(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    url = disposable_database
    config = make_alembic_config(url)
    command.upgrade(config, REVISION)
    execute(url, HELP_AREA_ANSWER)

    with pytest.raises(Exception, match="hold help areas"):
        command.downgrade(config, PREVIOUS)

    execute(url, "DELETE FROM intake_answers")
    command.downgrade(config, PREVIOUS)
    assert (
        scalar(
            url,
            "SELECT count(*) FROM information_schema.columns WHERE "
            "(table_name, column_name) IN (('session_types', 'is_starter'), "
            "('session_type_questions', 'options_source'), "
            "('intake_answers', 'selected_offering_id'))",
        )
        == 0
    )
    command.upgrade(config, REVISION)
    assert (
        scalar(
            url,
            "SELECT count(*) FROM pg_indexes "
            "WHERE indexname = 'ux_intake_answers_one_per_offering'",
        )
        == 1
    )
