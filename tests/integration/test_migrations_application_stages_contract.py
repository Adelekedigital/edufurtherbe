"""What `c4e7a9b2d5f8` leaves behind (decision 288, step 3b): the six and
nothing else, and no label. Split from `test_migrations.py` by area."""

import pytest
from alembic import command
from tests.integration.test_migrations import ConfigFactory, execute, scalar

pytestmark = pytest.mark.db

REVISION = "c4e7a9b2d5f8"
PREVIOUS = "056f597d551a"

SEED = """
INSERT INTO users (id, auth_id, email, primary_role, timezone)
VALUES ('00000000-0000-7000-8000-000000000001', gen_random_uuid(),
        'm@example.test', 'mentor', 'UTC');
INSERT INTO mentor_profiles (user_id) VALUES ('00000000-0000-7000-8000-000000000001');
INSERT INTO session_types (id, mentor_user_id, name)
VALUES ('00000000-0000-7000-8000-000000000002',
        '00000000-0000-7000-8000-000000000001', 'Chat');
"""

TYPE = "'00000000-0000-7000-8000-000000000002'"
SET_ROW = (
    "INSERT INTO session_type_stages (session_type_id, stage, position) "  # noqa: S608
    f"VALUES ({TYPE}, '{{}}', 0)"
)
LEGACY = (
    "UPDATE session_types SET application_stage = '{}', "  # noqa: S608
    f"custom_stage_label = {{}} WHERE id = {TYPE}"
)


@pytest.mark.parametrize("stage", ["drafting_stage", "other"])
def test_only_the_six_are_legal_and_no_label_is(
    disposable_database: str, make_alembic_config: ConfigFactory, stage: str
) -> None:
    config = make_alembic_config(disposable_database)
    command.upgrade(config, REVISION)
    execute(disposable_database, SEED)

    execute(disposable_database, SET_ROW.format("applying"))
    execute(disposable_database, LEGACY.format("applying", "NULL"))

    with pytest.raises(Exception, match="stage_is_known"):
        execute(disposable_database, SET_ROW.format(stage).replace(", 0)", ", 1)"))
    with pytest.raises(Exception, match="application_stage_is_known"):
        execute(disposable_database, LEGACY.format(stage, "'Gap year'"))
    with pytest.raises(Exception, match="no_custom_stage_label"):
        execute(disposable_database, LEGACY.format("applying", "'Gap year'"))


def test_the_downgrade_restores_the_expand_step(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """Twelve legal again, and `other` needs its label again."""
    config = make_alembic_config(disposable_database)
    command.upgrade(config, REVISION)
    execute(disposable_database, SEED)
    execute(disposable_database, SET_ROW.format("applying"))

    command.downgrade(config, PREVIOUS)

    execute(disposable_database, SET_ROW.format("revisions").replace(", 0)", ", 1)"))
    execute(disposable_database, LEGACY.format("other", "'Gap year'"))
    with pytest.raises(Exception, match="custom_stage_label_matches_stage"):
        execute(disposable_database, LEGACY.format("other", "NULL"))
    assert scalar(disposable_database, "SELECT count(*) FROM session_type_stages") == 2
