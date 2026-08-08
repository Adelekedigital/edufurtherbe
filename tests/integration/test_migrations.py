"""The migration chain itself.

Every test here runs against a database created for it and dropped afterwards,
and every one is synchronous: ``migrations/env.py`` calls ``asyncio.run``, which
raises ``RuntimeError`` if a loop is already running.
"""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

pytestmark = pytest.mark.db

FUNCTION_NAMES = ("set_updated_at", "uuid_generate_v7")

# Extensions the chain is expected to install, at head.
#
# `citext` is deliberately absent: M1 created it for `users.email` and
# `e25541374c03` dropped it again when normalisation moved to the boundary. The
# chain still creates and drops it in between, which is why this asserts the
# state at head rather than every extension the chain has ever touched.
# `pg_trgm` is the first extension whose *operators* the schema depends on:
# `pgcrypto` was declared for tidiness but `gen_random_uuid()` has been core
# since PostgreSQL 13, so a missing pgcrypto would never have surfaced. A missing
# pg_trgm fails loudly at `CREATE INDEX ... USING gin (name gin_trgm_ops)`.
EXTENSION_NAMES = ("pg_trgm", "pgcrypto")

# The exact set of tables at head, in the order `table_names` returns them.
#
# Asserting the exact list rather than a subset is what catches the reverse
# mistake: a table dropped, renamed, or never created because a migration was
# written but not added to the chain. It has to be updated in the same change
# that adds a table, which is the point — the same discipline as EXPECTED_MODELS
# in tests/unit/test_models.py.
EXPECTED_TABLES = [
    # M0 — reference data
    "countries",
    "languages",
    # M1 — identity
    "admin_users",
    "auth_identities",
    "legal_documents",
    "user_languages",
    "user_legal_consents",
    "user_onboarding",
    "user_profiles",
    "users",
    # M2 — the four lookups the profile tables reference
    "degree_levels",
    "institutions",
    "scholarship_programs",
    "service_offerings",
    # M2 — the profile tables themselves. `user_scholarship_experience` is
    # deliberately absent: no option set, no values, nothing to write it.
    "education_entries",
    "mentee_goal_countries",
    "mentee_goal_needs",
    "mentee_goals",
    "mentor_profiles",
    "mentor_service_offerings",
    "user_awards",
]

# Every enum type at head — five from M1, one from M2. Named here because a type
# outlives the table that used it: `DROP TABLE` does not remove one, so a
# downgrade that forgets leaves an orphan and the next upgrade fails with "type
# already exists".
#
# M2 adds only `lookup_status`. The other six vocabularies in `02_profiles.sql`
# are consumed by tables in the next pull request and ship with them, per settled
# decision #21 — a type no table uses is a schema asserting a choice nobody took.
ENUM_TYPE_NAMES = (
    "admin_role",
    "approval_status",
    "auth_provider",
    "language_proficiency",
    "legal_document_type",
    "listing_status",
    "lookup_status",
    "meeting_provider",
    "primary_role",
    "unlisted_reason",
    "verification_status",
)

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


def extension_names(url: str) -> list[str]:
    """The extensions this project installs, by name.

    Returns names rather than a count, and filters in Python rather than in an
    interpolated ``IN`` list: a count tells you something is missing, a name
    tells you which. The query itself takes no input, so there is no string
    building to get wrong.
    """

    async def _run() -> list[str]:
        conn = await asyncpg.connect(url)
        try:
            rows = await conn.fetch("SELECT extname FROM pg_extension ORDER BY extname")
            return [row["extname"] for row in rows if row["extname"] in EXTENSION_NAMES]
        finally:
            await conn.close()

    return asyncio.run(_run())


def enum_type_names(url: str) -> list[str]:
    """Enum types in the public schema, whether or not any table still uses one.

    Deliberately not scoped to types a column references — an orphaned type is
    exactly the failure being watched for, and scoping the query that way would
    make it invisible.
    """

    async def _run() -> list[str]:
        conn = await asyncpg.connect(url)
        try:
            rows = await conn.fetch(
                "SELECT t.typname FROM pg_type t "
                "JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE t.typtype = 'e' AND n.nspname = 'public' ORDER BY t.typname"
            )
            return [row["typname"] for row in rows]
        finally:
            await conn.close()

    return asyncio.run(_run())


def table_names(url: str) -> list[str]:
    async def _run() -> list[str]:
        conn = await asyncpg.connect(url)
        try:
            rows = await conn.fetch(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename <> 'alembic_version' "
                "ORDER BY tablename"
            )
            return [row["tablename"] for row in rows]
        finally:
            await conn.close()

    return asyncio.run(_run())


def test_chain_applies_to_an_empty_database(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """From empty, not from wherever the developer's database happens to be.

    A chain that only runs from the current revision is broken for every new
    environment, and nothing reveals that until someone tries to provision one.
    """
    command.upgrade(make_alembic_config(disposable_database), "head")

    assert has_pgcrypto(disposable_database)
    assert extension_names(disposable_database) == sorted(EXTENSION_NAMES)
    assert function_count(disposable_database) == len(FUNCTION_NAMES)
    assert sorted(table_names(disposable_database)) == sorted(EXPECTED_TABLES)
    assert enum_type_names(disposable_database) == sorted(ENUM_TYPE_NAMES)


def test_models_and_the_chain_agree(
    migrated_database: str, make_alembic_config: ConfigFactory
) -> None:
    """``alembic check`` — the drift detector that only exists once models do.

    It compares the models against the schema the chain actually produced, and
    raises ``AutogenerateDiffsDetected`` when they disagree. Before PR 2 this
    passed trivially: with no models the metadata was empty, so there was nothing
    to differ. A check that cannot fail is not a check, which is why it arrives
    here rather than with the harness.

    Its blind spots are real and stated in project-conventions: it sees tables,
    columns, types and regular indexes, and is blind to functions, triggers,
    partial indexes and CHECK constraints. Passing means the tables match, not
    that the migration is complete.
    """
    command.check(make_alembic_config(migrated_database))


def test_upgrade_downgrade_upgrade_is_clean(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """An untested downgrade is decoration, so this actually runs one.

    Extensions deliberately survive the downgrade; only the functions are
    dropped. Asserting that explicitly stops someone "fixing" the asymmetry
    without reading why it exists.

    **Enum types must NOT survive it**, and that is the assertion this test
    exists for now that M1 creates five of them. ``DROP TABLE`` removes a
    table's indexes, constraints and triggers but leaves an enum type standing —
    types are schema-level objects with their own lifetime, and autogenerate
    never emits the ``DROP TYPE``. The orphan is invisible until the *second*
    upgrade fails with ``type "primary_role" already exists``, which is why the
    re-upgrade below is not ceremony.
    """
    config = make_alembic_config(disposable_database)

    command.upgrade(config, "head")
    command.downgrade(config, "base")

    assert function_count(disposable_database) == 0
    assert has_pgcrypto(disposable_database)
    assert enum_type_names(disposable_database) == [], (
        "enum types outlived their tables; the next upgrade will fail on 'type already exists'"
    )

    command.upgrade(config, "head")

    assert function_count(disposable_database) == len(FUNCTION_NAMES)
    assert enum_type_names(disposable_database) == sorted(ENUM_TYPE_NAMES)


def schema_identifiers(url: str) -> list[str]:
    """Every constraint, index and enum type name the built schema holds."""

    async def _run() -> list[str]:
        conn = await asyncpg.connect(url)
        try:
            rows = await conn.fetch(
                "SELECT c.conname AS name FROM pg_constraint c "
                "JOIN pg_namespace n ON n.oid = c.connamespace WHERE n.nspname = 'public' "
                "UNION SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
                "UNION SELECT t.typname FROM pg_type t "
                "JOIN pg_namespace tn ON tn.oid = t.typnamespace "
                "WHERE t.typtype = 'e' AND tn.nspname = 'public'"
            )
            return sorted(row["name"] for row in rows)
        finally:
            await conn.close()

    return asyncio.run(_run())


#: Names the database holds that are never *declared* by that name in source.
#:
#: All three are the same case, which is why they belong together: a name the
#: naming convention produced from a shorter declared one — ``email_is_lowercase``
#: becomes ``ck_users_email_is_lowercase`` — plus alembic's own
#: ``alembic_version_pkc``. That is what the convention is for, so these are
#: exceptions to state rather than to discover.
#:
#: ``ck_users_email_is_lowercase`` was missing, and the test passed anyway. It
#: appears in source in exactly two places, both **prose**: a comment in an
#: unrelated migration explaining a historical double-render. A substring match
#: over file text cannot tell a declaration from a sentence about one, so tidying
#: that comment would have turned this red — and told the reader a 27-character
#: name had been truncated past 63 bytes. Found in review, not by the test.
UNSOURCED_IDENTIFIERS = frozenset(
    {
        "alembic_version_pkc",
        "ck_admin_users_revoker_implies_revocation",
        "ck_users_email_is_lowercase",
    }
)

SOURCE_ROOTS = (Path("src"), Path("migrations"))


def test_every_schema_identifier_appears_verbatim_in_source(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """Catches a **silently truncated** identifier, from any source.

    PostgreSQL's limit is 63 bytes and SQLAlchemy does not reject a longer name —
    it truncates and appends a deterministic hash, with no warning and no
    ``NOTICE``. The database then holds an identifier that appears nowhere in the
    repository, and the first ``op.drop_constraint`` written against the declared
    name fails with *constraint does not exist* — handed the wrong name by the
    very file somebody opened to look it up.

    Nothing else in the gate can see it: **``alembic check`` compares foreign
    keys by column signature, never by name**, and no other check reads
    constraint names at all. A 65-character key shipped this way through six
    green CI checks and a thirteen-way mutation batch, and a reviewer querying
    ``pg_constraint`` found it.

    ``test_no_declared_identifier_exceeds_the_postgresql_limit`` closes the same
    hole at declaration time by walking ``Base.metadata``. This one is wider on
    purpose: a name written directly into a **migration** and into no model is
    invisible to that test, and both migrations in M2 hand-write names.
    """
    command.upgrade(make_alembic_config(disposable_database), "head")

    source = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for root in SOURCE_ROOTS
        for path in root.rglob("*.py")
    )

    # A **quoted** occurrence, not a bare substring. Every identifier this test
    # is meant to protect is declared as a string literal — `Index("ix_…")`,
    # `name=op.f("fk_…")`, an enum label in `ENUM_TYPES` — so requiring the
    # quotes demands a declaration and rejects a passing mention.
    #
    # The looser version passed on a name that appears only inside a comment, and
    # would have gone red the day somebody edited that comment. A test that
    # depends on prose is a test that reports on the prose.
    missing = [
        name
        for name in schema_identifiers(disposable_database)
        if name not in UNSOURCED_IDENTIFIERS and f'"{name}"' not in source
    ]

    assert missing == [], (
        "these identifiers exist in the database but appear nowhere in src/ or "
        f"migrations/ — most likely silently truncated past 63 bytes: {missing}"
    )
