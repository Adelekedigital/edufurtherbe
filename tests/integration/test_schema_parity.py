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


PRIMARY_KEYS = """
SELECT c.relname AS tbl,
       array_agg(a.attname ORDER BY k.ord) AS pk_columns,
       pg_get_expr(d.adbin, d.adrelid) AS id_default
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
JOIN pg_constraint pc ON pc.conrelid = c.oid AND pc.contype = 'p'
CROSS JOIN LATERAL unnest(pc.conkey) WITH ORDINALITY AS k(attnum, ord)
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
WHERE c.relname <> 'alembic_version'
GROUP BY c.relname, d.adbin, d.adrelid
"""


def test_every_table_has_a_generated_surrogate_primary_key(migrated_database: str) -> None:
    """ADR 0014, and **this test is the decision** — the record only explains it.

    The rule "every table carries its own surrogate id" was already written, in
    tier-1 ``persistence-patterns``. It was overridden twice anyway — once for
    ISO lookups and once for 1:1 extensions and junction tables — and **every
    gate stayed green through both**, because nothing checked. A convention
    enforced only by prose is re-decided by whoever reads it next.

    So this walks the live schema rather than a list somebody maintains. Three
    assertions, because the three ways to break the rule are different:

    - a natural key (``countries.code``) — one PK column, but not ``id``
    - a composite key (``user_languages (user_id, language_code)``) — more
      than one PK column
    - an id the caller must supply — ``id``, sole column, but no default

    A new table cannot opt out of any of them by being forgotten here.
    """
    rows = query(migrated_database, PRIMARY_KEYS)

    assert rows, "no tables inspected; this assertion would otherwise pass on an empty schema"
    assert len(rows) == len(list(Base.registry.mappers)), (
        "the schema and the model registry disagree on table count"
    )

    composite = {r["tbl"]: r["pk_columns"] for r in rows if len(r["pk_columns"]) != 1}
    assert not composite, f"composite primary keys are not permitted: {composite}"

    natural = {r["tbl"]: r["pk_columns"][0] for r in rows if r["pk_columns"][0] != "id"}
    assert not natural, f"primary key must be named 'id': {natural}"

    ungenerated = {
        r["tbl"]: r["id_default"] or "(none)"
        for r in rows
        if "uuid_generate_v7" not in (r["id_default"] or "")
    }
    assert not ungenerated, (
        f"every id must be generated by the database, so no caller can supply one: {ungenerated}"
    )


def test_the_natural_keys_survive_as_unique_constraints(migrated_database: str) -> None:
    """Demoting a natural key to a plain column is how the invariant disappears.

    ``countries.code`` and ``languages.code_639_3`` were primary keys until ADR
    0014. A surrogate id does not make them non-unique — nothing does, unless
    somebody remembers to say so. Same story for the 1:1 and junction invariants:
    without these, two profiles per user and a duplicated language both become
    legal, silently.
    """
    rows = query(
        migrated_database,
        "SELECT conrelid::regclass::text AS tbl, pg_get_constraintdef(oid) AS def "
        "FROM pg_constraint WHERE contype = 'u' AND connamespace = 'public'::regnamespace "
        "UNION ALL "
        "SELECT tablename, indexdef FROM pg_indexes "
        "WHERE schemaname = 'public' AND indexdef LIKE 'CREATE UNIQUE INDEX%'",
    )
    unique = {(r["tbl"], r["def"]) for r in rows}

    required = {
        ("countries", "(code)"),
        ("languages", "(code_639_3)"),
        ("user_profiles", "(user_id)"),
        ("user_onboarding", "(user_id)"),
        ("user_languages", "(user_id, language_id)"),
        ("users", "(auth_id)"),
    }

    for table, columns in required:
        assert any(t == table and columns in d for t, d in unique), (
            f"{table}{columns} is not unique; the invariant its old primary key "
            f"carried has been lost"
        )


def test_the_models_package_is_what_this_file_inspects() -> None:
    """Guards the two tests above against inspecting an empty registry.

    Both walk ``Base.registry.mappers``. If the models package stopped exporting,
    they would iterate nothing and report green — the exact shape this repository
    has been bitten by more than once.
    """
    assert models.__all__
    assert len(list(Base.registry.mappers)) == len(models.__all__)
