"""Migrating in-process leaves the application's loggers on.

`migrations/env.py` reads `alembic.ini`'s logging section with `fileConfig`,
whose default disables every logger that already exists. Run in the test
process — every `db` test migrates a template — that silenced `app.api.errors`
for whichever tests a worker ran next, so a 500's "unhandled error" line
vanished depending on test order (PR #571's CI). Three tests carried a local
re-enable for the same cause; this is the one fix they were each working round.
"""

import logging

import pytest
from alembic import command
from tests.integration.test_migrations import ConfigFactory

pytestmark = pytest.mark.db


def test_an_existing_app_logger_survives_a_migration(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    logger = logging.getLogger("app.api.errors")
    logger.disabled = False

    command.upgrade(make_alembic_config(disposable_database), "head")

    assert logger.disabled is False
