"""DSN resolution and its failure modes.

No database is touched here — this is string handling and error behaviour, which
belongs in the fast tier.
"""

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.core.errors import AppError, ConfigurationError
from app.infra.db.engine import resolve_async_dsn, to_async_dsn


def settings_with(dsn: str | None) -> Settings:
    """``database_url`` is passed explicitly so an exported DATABASE_URL
    in the developer's shell cannot decide the outcome of these tests."""
    value = SecretStr(dsn) if dsn is not None else None
    return Settings(_env_file=None, environment="ci", database_url=value)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (
            "postgresql://u:p@host:5432/db",
            "postgresql+asyncpg://u:p@host:5432/db",
        ),
        # The legacy scheme Heroku popularised and several providers still emit.
        (
            "postgres://u:p@host:5432/db",
            "postgresql+asyncpg://u:p@host:5432/db",
        ),
    ],
)
def test_a_bare_scheme_gains_the_async_driver(given: str, expected: str) -> None:
    """SQLAlchemy picks its driver from the scheme, and a bare one selects the
    synchronous psycopg2 that this project does not install. The resulting import
    error names a package nobody added, a long way from the connection string
    that caused it."""
    assert to_async_dsn(given) == expected


def test_an_explicit_driver_is_left_alone() -> None:
    """An operator who wrote a driver meant it; rewriting would make their
    configuration a lie."""
    dsn = "postgresql+psycopg://u:p@host:5432/db"

    assert to_async_dsn(dsn) == dsn


def test_resolve_applies_the_rewrite() -> None:
    resolved = resolve_async_dsn(settings_with("postgresql://u:p@host:5432/db"))

    assert resolved == "postgresql+asyncpg://u:p@host:5432/db"


def test_a_missing_dsn_raises_configuration_error() -> None:
    with pytest.raises(ConfigurationError) as caught:
        resolve_async_dsn(settings_with(None))

    assert "DATABASE_URL" in str(caught.value)


def test_a_blank_dsn_raises_rather_than_producing_a_broken_url() -> None:
    """An empty string is a configured-but-wrong DSN, which is a different fault
    from an absent one and must not fall through to the rewrite."""
    with pytest.raises(ConfigurationError):
        resolve_async_dsn(settings_with("   "))


def test_configuration_error_is_an_app_error() -> None:
    """So one handler catches it, and so it can never be mapped to a 4xx by
    something that only knows about AppError subclasses."""
    assert issubclass(ConfigurationError, AppError)


@pytest.mark.parametrize("mode", ["disable", "prefer", "require", "verify-ca", "verify-full"])
def test_libpq_sslmode_is_renamed_to_the_asyncpg_spelling(mode: str) -> None:
    """A faithful rename, not a downgrade.

    SQLAlchemy's asyncpg dialect speaks ``ssl`` with the same vocabulary and the
    same meanings — ``ssl=verify-full`` fails on a missing root certificate
    rather than being ignored. Every mode is checked because translating only the
    common ones would silently drop the strict ones, which is the failure that
    would matter.
    """
    resolved = to_async_dsn(f"postgresql://u:p@host:5432/db?sslmode={mode}")

    assert resolved == f"postgresql+asyncpg://u:p@host:5432/db?ssl={mode}"


def test_an_explicit_psycopg_driver_keeps_its_libpq_query() -> None:
    """psycopg speaks libpq natively, so translating there would break a DSN that
    works. The rewrite applies only when the target is asyncpg."""
    dsn = "postgresql+psycopg://u:p@host:5432/db?sslmode=require"

    assert to_async_dsn(dsn) == dsn


def test_pgbouncer_is_rejected_rather_than_dropped() -> None:
    """It signals the transaction pooler, which needs NullPool and a disabled
    statement cache — an open question (ADR 0005). Dropping the parameter would
    remove the only sign that the question has gone live."""
    with pytest.raises(ConfigurationError) as caught:
        to_async_dsn("postgresql://u:p@host:5432/db?pgbouncer=true")

    message = str(caught.value)
    assert "pgbouncer" in message
    assert "ADR 0005" in message


def test_a_password_containing_a_percent_survives_the_query_rewrite() -> None:
    """The rewrite parses and rebuilds the URL, so it must not mangle the netloc.

    A literal ``%`` in a password is the character that breaks naive URL
    handling, and it is exactly what a generated Supabase password may contain.
    """
    resolved = to_async_dsn("postgresql://u:pa%%ss%word@host:5432/db?sslmode=require")

    assert resolved == "postgresql+asyncpg://u:pa%%ss%word@host:5432/db?ssl=require"


def test_a_dsn_without_a_query_is_untouched_beyond_the_scheme() -> None:
    assert to_async_dsn("postgresql://u:p@host:5432/db") == "postgresql+asyncpg://u:p@host:5432/db"


def test_the_dsn_is_not_exposed_by_the_settings_repr() -> None:
    """The DSN carries a password and reprs end up in tracebacks."""
    rendered = repr(settings_with("postgresql://u:hunter2@host:5432/db"))

    assert "hunter2" not in rendered
