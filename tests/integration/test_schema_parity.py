"""Facts about the built schema that ``alembic check`` cannot see.

`project-conventions` states the blind spots; this file is the part that closes
three of them. Each was **measured, not assumed** — the probes are recorded in
the docstrings so nobody has to re-derive why the test exists.

``alembic check`` runs ``compare_metadata``, and against a migrated database it
reports "No new upgrade operations detected" after every one of these:

- ``ALTER TABLE users ALTER COLUMN id SET DEFAULT uuid_generate_v7()``
- ``ALTER TYPE language_proficiency ADD VALUE 'expert'``
- a ``CHECK`` constraint landing under a different name than the model declares

The first two are silent until a user cannot log in or an insert fails with
``invalid input value for enum``. The third is silent until a later migration
drops a constraint by the name the model reports and fails in every environment.
"""

import asyncio
from collections.abc import Iterable

import asyncpg
import pytest
from sqlalchemy import CheckConstraint

from app.infra.db import models
from app.infra.db.base import Base
from app.infra.db.types import PG_ENUM_TYPES

pytestmark = pytest.mark.db


def query(url: str, sql: str) -> list[asyncpg.Record]:
    async def _run() -> list[asyncpg.Record]:
        conn = await asyncpg.connect(url)
        try:
            return await conn.fetch(sql)
        finally:
            await conn.close()

    return asyncio.run(_run())


def test_every_enum_type_matches_its_python_class(migrated_database: str) -> None:
    """Labels, not just type names.

    ``tests/integration/test_migrations.py`` asserts the five type *names* exist.
    That passes just as happily when a type is missing a label the Python class
    has — and adding a member to a ``StrEnum`` without a migration does exactly
    that, passing ruff, mypy, the layer check, ``alembic check`` and the whole
    suite before failing at the first insert that uses the new member.

    The comparison is by value, because ``pg_enum`` sends member values rather
    than member names; a regression there would show up here as every label
    disagreeing at once.
    """
    rows = query(
        migrated_database,
        "SELECT t.typname, e.enumlabel FROM pg_type t "
        "JOIN pg_enum e ON e.enumtypid = t.oid "
        "JOIN pg_namespace n ON n.oid = t.typnamespace WHERE n.nspname = 'public'",
    )

    actual: dict[str, set[str]] = {}
    for row in rows:
        actual.setdefault(row["typname"], set()).add(row["enumlabel"])

    assert PG_ENUM_TYPES, "no enum types registered; this test would inspect nothing"

    for enum_cls, type_name in PG_ENUM_TYPES.items():
        expected = {member.value for member in enum_cls}
        assert type_name in actual, f"{enum_cls.__name__} has no {type_name} type in the database"
        assert actual[type_name] == expected, (
            f"{enum_cls.__name__} and the {type_name} type disagree: "
            f"only in Python {expected - actual[type_name]}, "
            f"only in PostgreSQL {actual[type_name] - expected}"
        )


def model_check_constraint_names() -> Iterable[tuple[str, str]]:
    for mapper in Base.registry.mappers:
        table = mapper.class_.__table__
        for constraint in table.constraints:
            # Column-level NOT NULL arrives as an unnamed CheckConstraint on some
            # dialects; only named ones are ours to compare.
            if isinstance(constraint, CheckConstraint) and constraint.name:
                yield table.name, str(constraint.name)


def test_check_constraints_land_under_the_name_the_model_reports(migrated_database: str) -> None:
    """The failure this catches actually happened, and nothing else caught it.

    ``op.create_table`` applies ``Base.metadata.naming_convention``, whose ``ck``
    template is ``ck_%(table_name)s_%(constraint_name)s``. Passing the
    already-rendered ``ck_users_slug_is_url_safe`` to the migration therefore
    produced ``ck_users_ck_users_slug_is_url_safe`` in the database while the
    model kept reporting the single-prefixed name.

    ``compare_metadata`` does not diff CHECK constraints, so the whole suite
    stayed green. It would have surfaced at the first
    ``op.drop_constraint("ck_users_slug_is_url_safe", "users")`` — a migration
    that fails in every environment, which is precisely the hazard
    ``infra/db/base.py`` says the naming convention exists to prevent.

    ``pk``, ``uq`` and ``fk`` are unaffected: their templates carry no
    ``%(constraint_name)s`` token, so an explicit name is used verbatim. Only
    ``ck`` can double-render, which is why this test is scoped to it.
    """
    rows = query(
        migrated_database,
        "SELECT conrelid::regclass::text AS tbl, conname FROM pg_constraint "
        "WHERE contype = 'c' AND connamespace = 'public'::regnamespace",
    )
    in_database = {(row["tbl"], row["conname"]) for row in rows}
    declared = set(model_check_constraint_names())

    assert declared, "no named CHECK constraints found; this test would inspect nothing"

    missing = declared - in_database
    assert not missing, (
        f"declared by a model but absent from the database under that name: {sorted(missing)}. "
        f"CHECK constraints actually present: {sorted(in_database)}"
    )


def test_the_users_id_really_has_no_default_in_the_database(migrated_database: str) -> None:
    """The model-side assertion is not enough, and that is measured.

    ``tests/unit/test_models.py`` asserts ``User.id`` carries no default, which
    holds the *model*. ``migrations/env.py`` sets ``compare_type=True`` but not
    ``compare_server_default``, so a default added by a later migration or by
    hand is invisible to ``alembic check`` — verified against a migrated
    database, where ``alembic check`` reported no operations after the column was
    given ``uuid_generate_v7()``.

    ADR 0009 §9: ``users.id`` *is* the Supabase auth user id. A default mints a
    valid-looking id for any insert that forgot the real one, producing a row
    nobody can log into that looks entirely normal.
    """
    rows = query(
        migrated_database,
        "SELECT column_name, column_default FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'users' AND column_name = 'id'",
    )

    assert len(rows) == 1, "users.id not found; the query no longer describes the schema"
    assert rows[0]["column_default"] is None, (
        f"users.id has acquired a database default: {rows[0]['column_default']}"
    )


def test_every_other_id_does_have_a_database_default(migrated_database: str) -> None:
    """The counterweight. Asserting only that ``users.id`` has no default is
    satisfied by a schema where nothing generates ids, which would make every
    insert depend on the caller remembering — the trade ``users`` makes
    deliberately and no other table should."""
    tables = sorted(
        mapper.class_.__table__.name
        for mapper in Base.registry.mappers
        if "id" in mapper.class_.__table__.c and mapper.class_.__table__.name != "users"
    )
    rows = query(
        migrated_database,
        "SELECT table_name, column_default FROM information_schema.columns "
        "WHERE table_schema = 'public' AND column_name = 'id'",
    )
    defaults = {row["table_name"]: row["column_default"] for row in rows}

    assert tables, "no tables with an id column; this test would inspect nothing"

    for table in tables:
        assert defaults.get(table), f"{table}.id has no database default"
        assert "uuid_generate_v7" in defaults[table], (
            f"{table}.id defaults to {defaults[table]}, not uuid_generate_v7()"
        )


def test_the_models_package_is_what_this_file_inspects() -> None:
    """Guards the two tests above against inspecting an empty registry.

    Both walk ``Base.registry.mappers``. If the models package stopped exporting,
    they would iterate nothing and report green — the exact shape this repository
    has been bitten by more than once.
    """
    assert models.__all__
    assert len(list(Base.registry.mappers)) == len(models.__all__)
