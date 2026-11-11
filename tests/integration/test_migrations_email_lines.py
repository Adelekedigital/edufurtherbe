"""What the email wording migration leaves behind (PRD 28, decision 318).

Split from `test_migrations.py` by area, as the file-size limit asks
(non-negotiable 11); the helpers are imported from there, not copied.
"""

import asyncpg
import pytest
from alembic import command
from tests.integration.test_migrations import ConfigFactory, MigrateTo, execute, scalar

from app.domain.email_lines import LINE_HARD_MAX, LINE_KEY_MAX_LENGTH
from app.domain.enums import EmailEditAction, EmailLine

pytestmark = pytest.mark.db

REVISION = "c3f81a5d27e4"
PREVIOUS = "abee7fed06b7"

#: Whoever saved the line. A plain user is enough: the FK is to `users`, and
#: who may save is the endpoints' rule, not the tables'.
EDITOR = "00000000-0000-7000-8000-000000000001"
SEED_TEMPLATE = """
INSERT INTO users (id, auth_id, email, primary_role, timezone) VALUES
  ('{editor}', gen_random_uuid(), 'editor@example.test', 'mentor', 'UTC');
"""
SEED = SEED_TEMPLATE.format(editor=EDITOR)
OVERRIDE = (
    "INSERT INTO email_line_overrides "
    "(email_key, variant, line, override_text, default_at_edit, updated_by) "
    "VALUES ('{}', '{}', '{}', '{}', '{}', '{}')"
)
EDIT = (
    "INSERT INTO email_line_edits (email_key, variant, line, before_text, after_text, "
    "default_text, action, admin_id) VALUES ('{}', '{}', '{}', '{}', '{}', '{}', '{}', '{}')"
)
OVERRIDES = "SELECT count(*) FROM email_line_overrides"
EDITS = "SELECT count(*) FROM email_line_edits"
DELETE_USER = "DELETE FROM users WHERE id = '{}'"


def override(
    email_key: str = "credits_granted",
    variant: str = "default",
    line: str = "subject",
    text: str = "Your credits are here",
    default: str = "Your session credits are here",
) -> str:
    return OVERRIDE.format(email_key, variant, line, text, default, EDITOR)


def edit(
    line: str = "subject",
    before: str = "Old",
    after: str = "New",
    default: str = "Old",
    action: str = "edit",
    email_key: str = "credits_granted",
    variant: str = "default",
) -> str:
    return EDIT.format(email_key, variant, line, before, after, default, action, EDITOR)


def upgraded(url: str, migrate_to: MigrateTo) -> None:
    migrate_to(url, REVISION)
    execute(url, SEED)


def test_one_override_per_line_starting_at_version_one(
    disposable_database: str, migrate_to: MigrateTo
) -> None:
    upgraded(disposable_database, migrate_to)

    execute(disposable_database, override())
    assert scalar(
        disposable_database,
        "SELECT version = 1 AND id IS NOT NULL AND updated_at IS NOT NULL "
        "FROM email_line_overrides",
    )
    with pytest.raises(asyncpg.UniqueViolationError):
        execute(disposable_database, override(text="Another"))
    execute(disposable_database, override(line="heading"))
    execute(disposable_database, override(variant="mentor"))
    execute(disposable_database, override(email_key="credits_renewed"))
    assert scalar(disposable_database, OVERRIDES) == 4


@pytest.mark.parametrize(
    "bad",
    [
        {"line": "footer"},
        {"text": ""},
        {"default": ""},
        *(
            {"line": line, column: "x" * (LINE_HARD_MAX[line] + 1)}
            for line in EmailLine
            for column in ("text", "default")
        ),
        {"email_key": ""},
        {"email_key": "e" * (LINE_KEY_MAX_LENGTH + 1)},
        {"variant": ""},
        {"variant": "v" * (LINE_KEY_MAX_LENGTH + 1)},
    ],
)
def test_the_database_refuses_an_override_out_of_bounds(
    disposable_database: str, migrate_to: MigrateTo, bad: dict[str, str]
) -> None:
    upgraded(disposable_database, migrate_to)

    with pytest.raises(asyncpg.CheckViolationError):
        execute(disposable_database, override(**bad))
    assert scalar(disposable_database, OVERRIDES) == 0


def test_a_version_below_one_is_refused(disposable_database: str, migrate_to: MigrateTo) -> None:
    upgraded(disposable_database, migrate_to)
    execute(disposable_database, override())

    with pytest.raises(asyncpg.CheckViolationError):
        execute(disposable_database, "UPDATE email_line_overrides SET version = 0")


def test_the_bounds_are_the_domains_and_both_edges_are_allowed(
    disposable_database: str, migrate_to: MigrateTo
) -> None:
    upgraded(disposable_database, migrate_to)

    for line in EmailLine:
        most = "x" * LINE_HARD_MAX[line]
        execute(disposable_database, override(line=line, text=most, default=most))
        execute(disposable_database, edit(line=line, before=most, after=most, default=most))
    execute(
        disposable_database,
        override(email_key="e" * LINE_KEY_MAX_LENGTH, variant="v" * LINE_KEY_MAX_LENGTH, text="x"),
    )
    assert scalar(disposable_database, OVERRIDES) == len(EmailLine) + 1
    assert scalar(disposable_database, EDITS) == len(EmailLine)


@pytest.mark.parametrize("action", list(EmailEditAction))
def test_every_history_action_is_stored(
    disposable_database: str, migrate_to: MigrateTo, action: str
) -> None:
    upgraded(disposable_database, migrate_to)
    execute(disposable_database, edit(action=action))
    assert scalar(disposable_database, EDITS) == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"action": "delete"},
        {"line": "footer"},
        {"after": ""},
        {"email_key": ""},
        {"variant": "v" * (LINE_KEY_MAX_LENGTH + 1)},
        # Every text column, one past its line's max, on every line.
        *(
            {"line": line, column: "x" * (LINE_HARD_MAX[line] + 1)}
            for line in EmailLine
            for column in ("before", "after", "default")
        ),
    ],
)
def test_the_database_refuses_a_history_row_out_of_bounds(
    disposable_database: str, migrate_to: MigrateTo, bad: dict[str, str]
) -> None:
    upgraded(disposable_database, migrate_to)

    with pytest.raises(asyncpg.CheckViolationError):
        execute(disposable_database, edit(**bad))
    assert scalar(disposable_database, EDITS) == 0


def test_an_editor_with_wording_or_history_cannot_be_deleted(
    disposable_database: str, migrate_to: MigrateTo
) -> None:
    upgraded(disposable_database, migrate_to)
    execute(disposable_database, override())
    execute(disposable_database, edit())

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        execute(disposable_database, DELETE_USER.format(EDITOR))

    execute(disposable_database, "DELETE FROM email_line_overrides")
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        execute(disposable_database, DELETE_USER.format(EDITOR))


def test_an_update_moves_updated_at(disposable_database: str, migrate_to: MigrateTo) -> None:
    upgraded(disposable_database, migrate_to)
    execute(disposable_database, override())
    execute(
        disposable_database,
        "UPDATE email_line_overrides SET created_at = now() - interval '1 day', "
        "updated_at = now() - interval '1 day'",
    )

    execute(disposable_database, "UPDATE email_line_overrides SET version = version + 1")

    assert scalar(disposable_database, "SELECT updated_at > created_at FROM email_line_overrides")


def test_the_downgrade_drops_both_tables(
    disposable_database: str, make_alembic_config: ConfigFactory, migrate_to: MigrateTo
) -> None:
    upgraded(disposable_database, migrate_to)
    execute(disposable_database, override())
    execute(disposable_database, edit())

    command.downgrade(make_alembic_config(disposable_database), PREVIOUS)

    assert scalar(disposable_database, "SELECT to_regclass('email_line_overrides')") is None
    assert scalar(disposable_database, "SELECT to_regclass('email_line_edits')") is None
