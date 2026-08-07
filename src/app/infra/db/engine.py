"""Engine and session construction.

The only place the database URL is read. Nothing else calls
``settings.database_url.get_secret_value()``.
"""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings
from app.core.errors import ConfigurationError

ASYNC_DRIVER = "postgresql+asyncpg"

# Supabase, Heroku and most managed providers hand out a bare ``postgresql://``
# (and sometimes the legacy ``postgres://``). SQLAlchemy reads the scheme to pick
# a driver, so a bare scheme selects the *synchronous* psycopg2 driver, which is
# not installed — the failure is an obscure import error a long way from the
# connection string that caused it.
_BARE_SCHEMES = ("postgresql://", "postgres://")


# libpq query parameters that asyncpg does not accept. Without handling, each
# surfaces as `TypeError: connect() got an unexpected keyword argument 'sslmode'`
# from inside the driver — an error naming neither the connection string nor the
# driver choice that caused it.
#
# `sslmode` is a faithful rename. SQLAlchemy's asyncpg dialect speaks `ssl` with
# the same vocabulary and the same meanings — verified: `ssl=verify-full` fails on
# a missing root certificate rather than being ignored, so translating does not
# quietly weaken TLS.
_LIBPQ_RENAMES = {"sslmode": "ssl"}

# Rejected rather than dropped. `pgbouncer=true` says the DSN points at
# Supabase's transaction pooler, which needs `NullPool` and a disabled asyncpg
# statement cache — the pooling question ADR 0005 leaves open and this build
# deliberately does not guess at. Discarding the parameter would remove the only
# signal that the question has become live.
_LIBPQ_REJECTED = {
    "pgbouncer": (
        "indicates Supabase's transaction pooler, which needs NullPool and a "
        "disabled statement cache. That pooling decision is open (ADR 0005). "
        "Use the direct connection port, or settle the pooling mode first."
    ),
}

# THIS IS A DENYLIST, AND THAT IS ITS LIMIT. It covers the two parameters
# Supabase actually emits. Other libpq-only spellings — sslcert, sslrootcert,
# target_session_attrs — still reach asyncpg and still raise TypeError. Add them
# here when one is met rather than pre-empting the whole of libpq.


def _normalise_query(dsn: str) -> str:
    """Translate or reject libpq query parameters asyncpg cannot accept."""
    parts = urlsplit(dsn)
    if not parts.query:
        return dsn

    translated = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key in _LIBPQ_REJECTED:
            raise ConfigurationError(
                f"DATABASE_URL carries '{key}', which the asyncpg "
                f"driver cannot accept. It {_LIBPQ_REJECTED[key]}"
            )
        translated.append((_LIBPQ_RENAMES.get(key, key), value))

    return urlunsplit(parts._replace(query=urlencode(translated)))


def to_async_dsn(dsn: str) -> str:
    """Rewrite a bare PostgreSQL scheme to the async driver, and fix its query.

    An explicit driver (anything with a ``+`` in the scheme) is left alone: an
    operator who wrote ``postgresql+psycopg://`` meant it, and silently rewriting
    it would make their configuration a lie. Its query string is left alone too —
    psycopg speaks libpq natively, so translating ``sslmode`` there would break a
    DSN that works.
    """
    for scheme in _BARE_SCHEMES:
        if dsn.startswith(scheme):
            dsn = f"{ASYNC_DRIVER}://{dsn[len(scheme) :]}"
            break

    if ASYNC_DRIVER in dsn.split("://", 1)[0]:
        return _normalise_query(dsn)
    return dsn


def resolve_async_dsn(settings: Settings) -> str:
    """Return the configured DSN with an async driver, or raise if absent."""
    if settings.database_url is None:
        raise ConfigurationError(
            "DATABASE_URL is not set. The application needs a "
            "PostgreSQL connection string; see .env.example."
        )

    dsn = settings.database_url.get_secret_value().strip()
    if not dsn:
        raise ConfigurationError("DATABASE_URL is set but empty.")

    return to_async_dsn(dsn)


def create_database_engine(settings: Settings) -> AsyncEngine:
    """Build the engine.

    ``pool_pre_ping`` because managed Postgres closes idle connections without
    telling the client, and the first query on a dead connection fails rather
    than reconnecting. One extra round trip per checkout buys that away.

    ``echo`` stays off deliberately: SQLAlchemy's echo writes full statements
    including bound parameters, which for this schema means user emails and
    profile text in the application log.

    Pooling is unresolved (ADR 0005 lists it as an open question). Supabase's
    transaction-mode pooler is incompatible with client-side prepared statements,
    and using it will mean ``poolclass=NullPool`` plus a disabled asyncpg
    statement cache. Not configured here because it has not been tested against
    a real project, and guessing would produce a setting nobody can justify.
    """
    return create_async_engine(resolve_async_dsn(settings), pool_pre_ping=True, echo=False)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build the session factory.

    ``expire_on_commit=False`` because the default expires every attribute at
    commit, so the next attribute access re-queries — from synchronous context
    during serialization, which under asyncio raises ``MissingGreenlet`` and
    blames the schema rather than the commit that caused it.
    """
    return async_sessionmaker(engine, expire_on_commit=False)
