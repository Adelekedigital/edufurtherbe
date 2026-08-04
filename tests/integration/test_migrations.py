"""The migration chain itself.

Every test here runs against a database created for it and dropped afterwards,
and every one is synchronous: ``migrations/env.py`` calls ``asyncio.run``, which
raises ``RuntimeError`` if a loop is already running.
"""

import asyncio
from collections.abc import Callable
from typing import Any

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

pytestmark = pytest.mark.db

FUNCTION_NAMES = ("set_updated_at", "uuid_generate_v7")

ConfigFactory = Callable[[str], Config]


def scalar(url: str, sql: str) -> Any:
    async def _run() -> Any:
        conn = await asyncpg.connect(url)
        try:
            return await conn.fetchval(sql)
        finally:
            await conn.close()

    return asyncio.run(_run())


def function_count(url: str) -> int:
    count = scalar(
        url,
        "SELECT count(*) FROM pg_proc WHERE proname IN ('set_updated_at', 'uuid_generate_v7')",
    )
    return int(count)


def has_pgcrypto(url: str) -> bool:
    return scalar(url, "SELECT 1 FROM pg_extension WHERE extname = 'pgcrypto'") == 1


def test_chain_applies_to_an_empty_database(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """From empty, not from wherever the developer's database happens to be.

    A chain that only runs from the current revision is broken for every new
    environment, and nothing reveals that until someone tries to provision one.
    """
    command.upgrade(make_alembic_config(disposable_database), "head")

    assert has_pgcrypto(disposable_database)
    assert function_count(disposable_database) == len(FUNCTION_NAMES)


def test_upgrade_downgrade_upgrade_is_clean(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """An untested downgrade is decoration, so this actually runs one.

    The extension deliberately survives the downgrade; only the functions are
    dropped. Asserting that explicitly stops someone "fixing" the asymmetry
    without reading why it exists.
    """
    config = make_alembic_config(disposable_database)

    command.upgrade(config, "head")
    command.downgrade(config, "base")

    assert function_count(disposable_database) == 0
    assert has_pgcrypto(disposable_database)

    command.upgrade(config, "head")

    assert function_count(disposable_database) == len(FUNCTION_NAMES)
