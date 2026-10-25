"""What `6f3f4bbaf321` leaves behind, beyond "the chain runs".

Split from `test_migrations.py` by area, as the file-size limit asks
(non-negotiable 11); the helpers are imported from there, not copied.
"""

import pytest
from alembic import command
from tests.integration.test_migrations import ConfigFactory, scalar

from app.domain.session_type_limit import SESSION_TYPE_LIMIT_CEILING

pytestmark = pytest.mark.db

REVISION = "6f3f4bbaf321"
PREVIOUS = "e5b1c7d3a920"

COLUMN = (
    "SELECT count(*) FROM information_schema.columns "
    "WHERE table_name = 'mentor_profiles' AND column_name = 'session_type_limit'"
)


def test_session_type_limit_is_nullable_and_bounded(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """Null is "the default" and every existing row stays null; the bound is the
    domain's ceiling, so the migration's literal cannot drift from it."""
    config = make_alembic_config(disposable_database)
    command.upgrade(config, REVISION)

    assert scalar(disposable_database, COLUMN) == 1
    assert (
        scalar(
            disposable_database,
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'mentor_profiles' AND column_name = 'session_type_limit'",
        )
        == "YES"
    )
    assert scalar(
        disposable_database,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'ck_mentor_profiles_session_type_limit_sane'",
    ) == (
        "CHECK (((session_type_limit IS NULL) OR "
        f"((session_type_limit >= 1) AND (session_type_limit <= {SESSION_TYPE_LIMIT_CEILING}))))"
    )


def test_the_downgrade_removes_the_column(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    config = make_alembic_config(disposable_database)
    command.upgrade(config, REVISION)

    command.downgrade(config, PREVIOUS)

    assert scalar(disposable_database, COLUMN) == 0
    command.upgrade(config, REVISION)
    assert scalar(disposable_database, COLUMN) == 1
