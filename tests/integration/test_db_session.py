"""The session factory against a real database.

One smoke test. It exists to prove the wiring — engine, driver, pooling, session
— actually connects, because every later test that uses a session assumes it.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.infra.db.engine import create_session_factory

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def test_a_session_can_query_the_database(db_engine: AsyncEngine) -> None:
    session_factory = create_session_factory(db_engine)

    async with session_factory() as session:
        result = await session.execute(text("SELECT 1"))

        assert result.scalar_one() == 1


async def test_the_session_factory_does_not_expire_on_commit(db_engine: AsyncEngine) -> None:
    """``expire_on_commit=True`` is the default and is wrong under asyncio.

    It expires every attribute at commit, so the next attribute access re-queries
    — from synchronous context during serialization, where it raises
    ``MissingGreenlet`` and the traceback blames the schema rather than the
    commit that caused it.
    """
    session_factory = create_session_factory(db_engine)

    assert session_factory.kw["expire_on_commit"] is False
