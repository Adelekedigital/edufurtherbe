"""Postgres cancels what runs or waits too long, on every pooled connection."""

from __future__ import annotations

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import Settings
from app.infra.db.engine import apply_on_connect, create_database_engine, to_async_dsn

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def test_a_statement_over_the_timeout_is_cancelled_by_the_server(
    migrated_database: str,
) -> None:
    engine = create_database_engine(
        Settings(
            _env_file=None,
            database_url=SecretStr(migrated_database),
            database_statement_timeout_seconds=1,
        )
    )
    try:
        async with engine.connect() as conn:
            assert (await conn.execute(text("SHOW statement_timeout"))).scalar() == "1s"
            with pytest.raises(DBAPIError, match="statement timeout"):
                await conn.execute(text("SELECT pg_sleep(3)"))
        # The cancelled connection went back to the pool and still works.
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT 1"))).scalar() == 1
    finally:
        await engine.dispose()


async def test_a_statement_inside_the_timeout_completes(migrated_database: str) -> None:
    engine = create_database_engine(
        Settings(_env_file=None, database_url=SecretStr(migrated_database))
    )
    try:
        async with engine.connect() as conn:
            assert (await conn.execute(text("SHOW statement_timeout"))).scalar() == "1min"
            assert (await conn.execute(text("SELECT pg_sleep(0.1), 1"))).one()[1] == 1
    finally:
        await engine.dispose()


async def test_a_parameter_set_on_connect_holds_across_transactions(
    migrated_database: str,
) -> None:
    """What migrations rely on for `lock_timeout`: session-level, so a commit
    inside the run does not drop it."""
    engine = create_async_engine(to_async_dsn(migrated_database), pool_size=1, max_overflow=0)
    apply_on_connect(engine, lock_timeout="7s")
    try:
        for _ in range(2):
            async with engine.begin() as conn:
                assert (await conn.execute(text("SHOW lock_timeout"))).scalar() == "7s"
    finally:
        await engine.dispose()
