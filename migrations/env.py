"""Alembic environment.

Two things differ from the stock async template, both deliberate.

**The URL comes from ``core/config.py``, not from ``alembic.ini``.** Migrations
connect with the same credential, read the same way, as the application. A DSN in
the ini file would be a second configuration source that git tracks.

**The URL is injected into the config *section*, not via
``config.set_main_option``.** ConfigParser applies ``%``-interpolation to values
set that way, so a password containing a literal ``%`` raises an
``InterpolationSyntaxError`` from somewhere far away from the password. Passing a
plain dict skips interpolation entirely.

Nothing here is type-checked: ``mypy src`` and the pre-commit hook's
``files: ^src/`` both stop at the package boundary, and this file sits outside
it. It is linted and formatted by ruff like everything else.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings
from app.infra.db.base import Base
from app.infra.db.engine import resolve_async_dsn, to_async_dsn

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Autogenerate compares against this. It is empty until the first model lands in
# PR 2 — which is correct, not an oversight: an empty metadata means autogenerate
# would propose dropping every table, so autogenerate must not be run until there
# are models. The M0 migration is hand-written for that reason among others.
target_metadata = Base.metadata

# `config.attributes["dsn"]` is how a caller injects a URL programmatically — the
# tests use it to point each run at a database created for that test. It is
# preferred over the setting so that no test has to mutate os.environ and clear
# the get_settings cache, which under pytest-randomly leaks into whatever runs
# next. Alembic never populates it itself, so the CLI always falls through to the
# configured setting, which raises ConfigurationError with an actionable message
# when it is absent.
#
# Not `config.set_main_option`: that path is ConfigParser-backed and applies
# %-interpolation, so a password containing a literal % fails far from its cause.
_dsn_override = config.attributes.get("dsn")
DATABASE_URL = to_async_dsn(_dsn_override) if _dsn_override else resolve_async_dsn(get_settings())


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing it (``alembic upgrade --sql``)."""
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Off by default, and worth having: a column whose type drifted from the
        # model is otherwise invisible to `alembic check`.
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an engine and run the migrations against a live connection."""
    section = dict(config.get_section(config.config_ini_section, {}) or {})
    section["sqlalchemy.url"] = DATABASE_URL

    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations against a database.

    ``asyncio.run`` is why every test that invokes alembic must be synchronous —
    calling this from inside a running event loop raises ``RuntimeError``.
    """
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
