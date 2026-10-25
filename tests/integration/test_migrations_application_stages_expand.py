"""What `a1f4c8e2d6b9` leaves behind, beyond "the chain runs" (Session Types
PRD, founder 2026-10-21): the design's six stages are legal beside the old six.

Split from `test_migrations.py` by area (non-negotiable 11); the helpers are
imported from there, not copied.
"""

import pytest
from alembic import command
from tests.integration.test_migrations import ConfigFactory, execute, scalar

pytestmark = pytest.mark.db

REVISION = "a1f4c8e2d6b9"
PREVIOUS = "d4a9e2c7b5f1"

OLD = (
    "early_exploration",
    "drafting_stage",
    "post_submission",
    "revisions",
    "interviewing",
    "other",
)
NEW = (
    "exploring",
    "researching",
    "preparing_documents",
    "applying",
    "interview_prep",
    "awaiting_decision",
)

#: One session type, built in SQL: the app's factories need head.
SEED = """
INSERT INTO users (id, auth_id, email, primary_role, timezone)
VALUES ('00000000-0000-7000-8000-000000000001', gen_random_uuid(),
        'm@example.test', 'mentor', 'Africa/Lagos');
INSERT INTO mentor_profiles (user_id) VALUES ('00000000-0000-7000-8000-000000000001');
INSERT INTO session_types (id, mentor_user_id, name)
VALUES ('00000000-0000-7000-8000-000000000002',
        '00000000-0000-7000-8000-000000000001', 'Chat');
"""

TYPE = "'00000000-0000-7000-8000-000000000002'"


def stage_row(stage: str, position: int) -> str:
    """Values are this file's own constants, never input."""
    return (
        "INSERT INTO session_type_stages (session_type_id, stage, position) "  # noqa: S608
        f"VALUES ({TYPE}, '{stage}', {position})"
    )


def legacy(stage: str) -> str:
    """The legacy single column; `other` carries its label, as its CHECK asks."""
    label = "'Custom'" if stage == "other" else "NULL"
    return (
        f"UPDATE session_types SET application_stage = '{stage}', "  # noqa: S608
        f"custom_stage_label = {label} WHERE id = {TYPE}"
    )


def test_both_vocabularies_are_legal_and_nothing_else_is(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    config = make_alembic_config(disposable_database)
    command.upgrade(config, REVISION)
    execute(disposable_database, SEED)

    for position, stage in enumerate(OLD + NEW):
        execute(disposable_database, stage_row(stage, position))
        execute(disposable_database, legacy(stage))

    assert scalar(disposable_database, "SELECT count(*) FROM session_type_stages") == 12
    with pytest.raises(Exception, match="stage_is_known"):
        execute(disposable_database, stage_row("wizard", 99))
    with pytest.raises(Exception, match="application_stage_is_known"):
        execute(disposable_database, legacy("wizard"))


def test_the_downgrade_narrows_back_when_no_row_holds_a_new_stage(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    config = make_alembic_config(disposable_database)
    command.upgrade(config, REVISION)
    execute(disposable_database, SEED)
    execute(disposable_database, stage_row("revisions", 0))

    command.downgrade(config, PREVIOUS)

    with pytest.raises(Exception, match="stage_is_known"):
        execute(disposable_database, stage_row("exploring", 1))
    assert scalar(disposable_database, "SELECT stage FROM session_type_stages") == "revisions"


@pytest.mark.parametrize("where", ["set", "legacy"])
def test_the_downgrade_refuses_while_a_row_holds_a_new_stage(
    disposable_database: str, make_alembic_config: ConfigFactory, where: str
) -> None:
    """`researching` has no old equivalent; refusing beats dropping a choice."""
    config = make_alembic_config(disposable_database)
    command.upgrade(config, REVISION)
    execute(disposable_database, SEED)
    execute(
        disposable_database,
        stage_row("researching", 0) if where == "set" else legacy("researching"),
    )

    with pytest.raises(Exception, match="remap them before downgrading"):
        command.downgrade(config, PREVIOUS)
