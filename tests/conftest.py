"""Fixtures shared across every tier."""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.infra.db.engine import create_database_engine
from app.main import create_app

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Deliberately NOT prefixed with EDUFURTHER_.
#
# `Settings.reject_unknown_prefixed_variables` fails startup on any EDUFURTHER_
# variable that is not a declared field, so a EDUFURTHER_TEST_DATABASE_URL would
# break every test that constructs Settings — including the ones that have
# nothing to do with a database. These are test-harness variables, not
# application configuration, and the no-inline-os.environ house rule governs the
# latter.
DB_URL_ENV = "TEST_DATABASE_URL"
REQUIRE_DB_ENV = "REQUIRE_DB_TESTS"

_SKIP_REASON = (
    f"No database. Start one with `docker compose up -d` and set "
    f"{DB_URL_ENV}=postgresql://edufurther:edufurther@localhost:55432/edufurther"
)


@pytest.fixture
def settings() -> Settings:
    """Explicit settings, so a test never depends on the developer's environment.

    ``_env_file=None`` is load-bearing. Without it the fixture still reads
    whatever ``.env`` happens to sit in the working directory, and any field not
    pinned below silently takes that file's value — init arguments outrank a
    dotenv value, so the fields named here hid the leak rather than preventing it.
    """
    return Settings(_env_file=None, environment="ci", debug=False)


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _sync_dsn(url: str) -> str:
    """Strip any SQLAlchemy driver suffix — asyncpg.connect wants a bare DSN."""
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


@pytest.fixture(scope="session")
def admin_database_url() -> str:
    """The maintenance connection, or skip.

    ``REQUIRE_DB_TESTS=1`` turns the skip into a failure. CI sets it, so a
    misconfigured runner cannot quietly report green with the entire database
    tier absent — a skipped test and a passing test are indistinguishable in a
    summary line, and this repository has been bitten by that shape before.
    """
    url = os.environ.get(DB_URL_ENV)
    if url:
        return _sync_dsn(url)
    if os.environ.get(REQUIRE_DB_ENV) == "1":
        pytest.fail(f"{REQUIRE_DB_ENV}=1 but {DB_URL_ENV} is unset. {_SKIP_REASON}")
    pytest.skip(_SKIP_REASON)


@pytest.fixture
def disposable_database(admin_database_url: str) -> Iterator[str]:
    """An empty database of its own, dropped afterwards.

    Per-test rather than shared because ``pytest-randomly`` reorders the suite:
    a migration test that downgrades a database another test expects at head
    fails only on some seeds, which is the worst kind of flake to diagnose.

    ``CREATE DATABASE`` cannot run inside a transaction or take bind parameters,
    hence the raw connection and the interpolated name. The name is built here
    from a uuid4 hex string, so it is never caller-controlled.
    """
    name = f"test_{uuid.uuid4().hex[:16]}"
    base, _, _ = admin_database_url.rpartition("/")

    async def _create() -> None:
        conn = await asyncpg.connect(admin_database_url)
        try:
            await conn.execute(f'CREATE DATABASE "{name}"')
        finally:
            await conn.close()

    async def _drop() -> None:
        conn = await asyncpg.connect(admin_database_url)
        try:
            await conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                name,
            )
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            await conn.close()

    asyncio.run(_create())
    try:
        yield f"{base}/{name}"
    finally:
        asyncio.run(_drop())


def _alembic_config(url: str) -> Config:
    """An Alembic config pointed at ``url``.

    The URL travels on ``config.attributes``, which ``env.py`` prefers over the
    setting. Nothing here touches ``os.environ`` or the ``get_settings`` cache:
    a process-wide mutation would outlive the test that made it, and under
    ``pytest-randomly`` the test it then affects is different on every seed.
    """
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["dsn"] = url
    return config


@pytest.fixture
def make_alembic_config() -> Callable[[str], Config]:
    """Exposed as a fixture because ``tests`` is not an importable package.

    There is no ``tests/__init__.py``, and adding one to share a helper would
    change import semantics for every module in the suite. A fixture is the
    smaller change.
    """
    return _alembic_config


@pytest.fixture
def migrated_database(disposable_database: str) -> Iterator[str]:
    """A disposable database with the full migration chain applied.

    Synchronous on purpose: ``env.py`` calls ``asyncio.run``, which raises if a
    loop is already running. Keeping every alembic invocation in sync fixtures
    and sync tests is what makes that safe.
    """
    command.upgrade(_alembic_config(disposable_database), "head")
    yield disposable_database


@pytest_asyncio.fixture
async def db_engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    """An engine bound to a migrated, disposable database.

    ``Settings`` is constructed directly rather than read from the environment,
    so this fixture neither depends on nor disturbs the process environment.
    """
    engine = create_database_engine(
        Settings(_env_file=None, database_url=SecretStr(migrated_database))
    )
    try:
        yield engine
    finally:
        await engine.dispose()
