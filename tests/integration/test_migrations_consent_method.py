"""What `b7c2e4a91f30` changes (decision 345): `analytics_consents.method`,
null on rows that predate it, and the database refusing a method that is not
one of the four or does not fit the answer, for writers that bypass the API.
"""

import asyncpg
import pytest
from alembic import command
from tests.integration.test_migrations import ConfigFactory, MigrateTo, execute, scalar

pytestmark = pytest.mark.db

REVISION = "b7c2e4a91f30"
PREVIOUS = "e8b2c5f1a7d3"

SEED = """
INSERT INTO users (email) VALUES ('member@example.test');
INSERT INTO analytics_consents (user_id, granted, version)
  SELECT id, false, 'v1' FROM users WHERE email = 'member@example.test';
"""


_SET = "UPDATE analytics_consents SET "

#: Each answer as a whole literal statement, so no SQL is built from values.
ANSWERS = {
    ("accept", True): _SET + "method = 'accept', granted = true",
    ("accept", False): _SET + "method = 'accept', granted = false",
    ("decline", True): _SET + "method = 'decline', granted = true",
    ("decline", False): _SET + "method = 'decline', granted = false",
    ("close", True): _SET + "method = 'close', granted = true",
    ("close", False): _SET + "method = 'close', granted = false",
    ("settings", True): _SET + "method = 'settings', granted = true",
    ("settings", False): _SET + "method = 'settings', granted = false",
    ("banner", False): _SET + "method = 'banner', granted = false",
}


def answer(method: str, granted: bool) -> str:
    return ANSWERS[method, granted]


def upgraded(url: str, migrate_to: MigrateTo, config: ConfigFactory) -> None:
    migrate_to(url, PREVIOUS)
    execute(url, SEED)
    command.upgrade(config(url), REVISION)


def test_an_existing_answer_keeps_a_null_method(
    disposable_database: str, make_alembic_config: ConfigFactory, migrate_to: MigrateTo
) -> None:
    upgraded(disposable_database, migrate_to, make_alembic_config)

    assert scalar(disposable_database, "SELECT count(*) FROM analytics_consents") == 1
    assert scalar(disposable_database, "SELECT method FROM analytics_consents") is None


@pytest.mark.parametrize(
    ("method", "granted"),
    [
        ("accept", True),
        ("decline", False),
        ("close", False),
        ("settings", True),
        ("settings", False),
    ],
)
def test_a_method_that_fits_its_answer_is_stored(
    disposable_database: str,
    make_alembic_config: ConfigFactory,
    migrate_to: MigrateTo,
    method: str,
    granted: bool,
) -> None:
    upgraded(disposable_database, migrate_to, make_alembic_config)

    execute(disposable_database, answer(method, granted))

    assert scalar(disposable_database, "SELECT method FROM analytics_consents") == method


@pytest.mark.parametrize(
    ("method", "granted", "constraint"),
    [
        ("banner", False, "ck_analytics_consents_method_is_known"),
        ("accept", False, "ck_analytics_consents_method_fits_answer"),
        ("decline", True, "ck_analytics_consents_method_fits_answer"),
        ("close", True, "ck_analytics_consents_method_fits_answer"),
    ],
)
def test_the_database_refuses_an_unknown_or_misfit_method(
    disposable_database: str,
    make_alembic_config: ConfigFactory,
    migrate_to: MigrateTo,
    method: str,
    granted: bool,
    constraint: str,
) -> None:
    upgraded(disposable_database, migrate_to, make_alembic_config)

    with pytest.raises(asyncpg.CheckViolationError, match=constraint):
        execute(disposable_database, answer(method, granted))


def test_the_downgrade_drops_the_column_and_keeps_the_answer(
    disposable_database: str, make_alembic_config: ConfigFactory, migrate_to: MigrateTo
) -> None:
    upgraded(disposable_database, migrate_to, make_alembic_config)
    execute(disposable_database, answer("close", False))

    command.downgrade(make_alembic_config(disposable_database), PREVIOUS)

    assert (
        scalar(
            disposable_database,
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'analytics_consents' AND column_name = 'method'",
        )
        == 0
    )
    assert scalar(disposable_database, "SELECT count(*) FROM analytics_consents") == 1
