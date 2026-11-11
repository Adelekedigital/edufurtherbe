"""A test's database work, rolled back instead of dropped.

**The recipe** is SQLAlchemy 2.0's "Joining a Session into an External
Transaction (such as for test suites)": one connection holds an outer
transaction, everything the test does runs in SAVEPOINTs inside it, and the
outer transaction is rolled back at teardown
(https://docs.sqlalchemy.org/en/20/orm/session_transaction.html#joining-a-session-into-an-external-transaction-such-as-for-test-suites).

**One level down from the documented form.** That page binds each ``Session`` to
the test's ``Connection`` with ``join_transaction_mode="create_savepoint"``.
This suite binds to an *engine*: ``db_engine`` reaches over a thousand call
sites (``engine.begin()``, ``engine.connect()``, ``AsyncSession(engine)``, every
store, ``create_session_factory``), so the savepoint is made where every one of
them meets: the driver. The engine below hands each checkout a fresh SQLAlchemy
adapter over the *same* asyncpg connection, and that connection's
``transaction()``, which the adapter begins and ends every transaction with,
is a SAVEPOINT in the outer one. So:

- ``engine.begin()`` commits by RELEASE and rolls back by ROLLBACK TO, as a
  real commit or rollback would, but only inside the outer transaction;
- a checkout closed without committing is rolled back to its savepoint, as a
  pool's reset-on-return would;
- a session's ``commit()``/``rollback()`` is the same, through its connection;
- committing a transaction a statement failed in rolls it back silently, as
  PostgreSQL's ``COMMIT`` of an aborted transaction does.

**What this cannot model**, which is what ``@pytest.mark.isolated_db`` is for:
``now()`` is frozen for the whole test (it is the transaction's start), and
there is one backend, so nothing behaves as two connections would: a second
connection would not see uncommitted rows, would wait on locks, and would
keep its commit when the first rolls back. Two transactions open at once are
therefore refused (``OVERLAP``) rather than run in one, and an ``isolated_db``
test gets a database of its own.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.infra.db.engine import CONNECT_TIMEOUT, create_database_engine

#: Why a checkout's transaction was refused: the case this harness cannot model.
OVERLAP = (
    "two transactions are open at once on what would be two connections: one "
    "would not see the other's uncommitted rows or wait on its locks, and its "
    "commit would survive the other's rollback. One connection cannot model "
    "that; mark the test @pytest.mark.isolated_db"
)


async def run_sql(url: str, sql: str, *args: Any) -> Any:
    """One statement on a connection of its own; its first value, if any.

    The suite's one way to reach a database outside an engine: creating and
    dropping databases, and ending a backend from outside it.
    """
    conn = await asyncpg.connect(url, timeout=CONNECT_TIMEOUT)
    try:
        return await conn.fetchval(sql, *args)
    finally:
        await conn.close()


class _Savepoint:
    """A checkout's transaction: a SAVEPOINT in the test's outer transaction.

    The shape of ``asyncpg.transaction.Transaction``, which is all the
    SQLAlchemy adapter calls. An isolation level or a read-only transaction
    cannot be had inside another transaction, so asking for one fails loudly:
    that test needs ``isolated_db``.
    """

    def __init__(
        self,
        shared: _Shared,
        name: str,
        *,
        isolation: str | None = None,
        readonly: bool = False,
        deferrable: bool = False,
    ) -> None:
        if isolation is not None or readonly or deferrable:
            raise asyncpg.InterfaceError(
                "a savepoint cannot change isolation, read-only or deferrable; "
                "mark the test @pytest.mark.isolated_db"
            )
        self._shared = shared
        self._name = name

    async def start(self) -> None:
        self._shared.open(self)
        await self._shared.connection.execute(f"SAVEPOINT {self._name}")

    async def commit(self) -> None:
        self._shared.close_savepoint(self)
        try:
            await self._shared.connection.execute(f"RELEASE SAVEPOINT {self._name}")
        except asyncpg.InFailedSQLTransactionError:
            await self._roll_back()

    async def rollback(self) -> None:
        self._shared.close_savepoint(self)
        await self._roll_back()

    async def _roll_back(self) -> None:
        await self._shared.connection.execute(
            f"ROLLBACK TO SAVEPOINT {self._name}; RELEASE SAVEPOINT {self._name}"
        )


class _Shared:
    """The test's one asyncpg connection, as every checkout sees it.

    Closing a checkout must not close the connection the next checkout and the
    outer transaction are on, and a checkout's transaction is a savepoint.
    **One at a time**: a second checkout's transaction opened while another's
    is still open is refused (``OVERLAP``), so a test that depends on what two
    connections do differently fails loudly instead of passing for the wrong
    reason.
    """

    def __init__(self, connection: asyncpg.Connection) -> None:
        self.connection = connection
        self._savepoints = 0
        self._open: _Savepoint | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.connection, name)

    def transaction(self, **options: Any) -> _Savepoint:
        self._savepoints += 1
        return _Savepoint(self, f"test_sp_{self._savepoints}", **options)

    def open(self, savepoint: _Savepoint) -> None:
        if self._open is not None:
            raise AssertionError(OVERLAP)
        self._open = savepoint

    def close_savepoint(self, savepoint: _Savepoint) -> None:
        if self._open is savepoint:
            self._open = None

    async def close(self, *, timeout: float | None = None) -> None:
        """Kept open: the fixture closes it, after the outer rollback."""

    def terminate(self) -> None:
        """Kept open, as ``close``."""


class _Outer:
    """The test's connection and its outer transaction, opened on first checkout.

    **Lazily, on the loop the test runs on.** The anyio-marked suites run their
    tests on anyio's loop while ``db_engine`` is set up on pytest-asyncio's, and
    an asyncpg connection is bound to the loop that opened it, as a pooled
    engine's connections always were.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._shared: _Shared | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._opening = asyncio.Lock()

    async def checkout(self) -> _Shared:
        async with self._opening:  # two first checkouts at once open one connection
            if self._shared is None:
                connection = await asyncpg.connect(self._dsn, timeout=CONNECT_TIMEOUT)
                # Read-write against the database's read-only default (`worker_database`).
                await connection.execute("BEGIN READ WRITE")
                self._loop = asyncio.get_running_loop()
                self._shared = _Shared(connection)
        return self._shared

    async def roll_back(self) -> None:
        """Roll the outer transaction back and close.

        **When that cannot be done in place** the server ends the backend
        instead: ``pg_terminate_backend`` aborts its transaction and waits until
        it is gone, so no lock it held reaches the next test. That is the path
        from another loop (an anyio test's, closed by now, so the connection
        cannot be awaited or even written to: its transport is left to the
        garbage collector), and after a failed ROLLBACK.
        """
        if self._shared is None:
            return
        connection = self._shared.connection
        if self._loop is asyncio.get_running_loop():
            try:
                await connection.execute("ROLLBACK")
                await connection.close()
                return
            except BaseException:
                await self._end_backend(connection.get_server_pid())
                raise
        await self._end_backend(connection.get_server_pid())

    async def _end_backend(self, pid: int) -> None:
        if not await run_sql(self._dsn, "SELECT pg_terminate_backend($1, 10000)", pid):
            raise RuntimeError("the test's connection outlived its teardown")


@asynccontextmanager
async def rolled_back_engine(settings: Settings, dsn: str) -> AsyncIterator[AsyncEngine]:
    """An engine whose every transaction is a savepoint in one that never commits.

    Built by ``create_database_engine`` so it carries the production engine's
    options (statement timeout and all); only the pool differs. ``NullPool``
    because each checkout must be a new adapter, with its own transaction state,
    over the shared connection: a pooled adapter would carry one checkout's
    transaction into the next.
    """
    outer = _Outer(dsn)
    engine = create_database_engine(settings, poolclass=NullPool, async_creator=outer.checkout)
    try:
        yield engine
    finally:
        try:
            await engine.dispose()
        finally:
            await outer.roll_back()
