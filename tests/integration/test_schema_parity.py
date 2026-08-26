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
import re
from collections.abc import Iterable

import asyncpg
import pytest
from sqlalchemy import CheckConstraint

from app.infra.db import models
from app.infra.db.base import Base
from app.infra.db.types import TEXT_CHECK_ENUMS, StrEnumText

pytestmark = pytest.mark.db


def query(url: str, sql: str) -> list[asyncpg.Record]:
    async def _run() -> list[asyncpg.Record]:
        conn = await asyncpg.connect(url)
        try:
            return await conn.fetch(sql)
        finally:
            await conn.close()

    return asyncio.run(_run())


def test_no_postgresql_enum_type_survives(migrated_database: str) -> None:
    """The replacement for ``test_every_enum_type_matches_its_python_class``.

    That test compared each PostgreSQL enum's labels against its Python class,
    because ``alembic check`` is blind to enum labels. Settled decision #100
    finished in step 8 and there are no enum types left to compare, so the
    assertion inverts: **none may exist**.

    It is not a test that passes by inspecting nothing. It fails the moment a
    migration creates a type — which is exactly the regression to guard, since
    the deferred ``calendar_connections`` and ``search_impressions_suppressed``
    tables both declare enums in the canonical DDL, and building either verbatim
    would reintroduce one with every other gate green.
    """
    rows = query(
        migrated_database,
        "SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
        "WHERE n.nspname = 'public' AND t.typtype = 'e'",
    )

    assert [row["typname"] for row in rows] == [], (
        "settled decision #100 removed every PostgreSQL enum type; a new one is a "
        "regression, and takes text + CHECK instead"
    )


def converted_columns() -> list[tuple[str, str, type]]:
    """``(table, column, enum class)`` for every column declared as ``text``.

    Read off the models rather than from a registry, because the models are what
    the database is built from. ``TEXT_CHECK_ENUMS`` carries only the constraint
    *name*; asking it which columns converted would be asking the same question
    twice and letting the two answers drift.
    """
    found: list[tuple[str, str, type]] = []
    for table in Base.metadata.tables.values():
        for column in table.c:
            if isinstance(column.type, StrEnumText):
                found.append((table.name, column.name, column.type.enum_cls))
    return found


def test_every_converted_enum_has_a_check_naming_its_values(migrated_database: str) -> None:
    """Settled decision #100's actual guarantee, asserted against the database.

    The ``StrEnum`` at the Pydantic boundary is not the control. **The ETL and
    ``scripts/load_*.py`` write these columns with hand-written SQL and never
    construct a model**, so the ``CHECK`` is the only thing standing between a
    transform bug and ``'Mentor'`` or ``'wizard'`` in a closed vocabulary. This
    is the test that says it is really there.

    ``alembic check`` cannot do it: ``compare_metadata`` does not diff CHECK
    constraints at all, so a migration that converted a column and forgot its
    constraint leaves the whole gate green and the column unguarded — strictly
    worse than the enum it replaced.

    Values are compared, not just presence. A constraint naming three of four
    members refuses a legitimate value in production and nothing else would
    notice until a user hit it.
    """
    columns = converted_columns()

    assert columns, (
        "no converted columns found; this test would inspect nothing. Once a "
        "vocabulary is in TEXT_CHECK_ENUMS its column must use StrEnumText"
    )
    assert {cls for _, _, cls in columns} == set(TEXT_CHECK_ENUMS), (
        "TEXT_CHECK_ENUMS and the StrEnumText columns disagree about which "
        "vocabularies have converted"
    )

    rows = query(
        migrated_database,
        "SELECT conrelid::regclass::text AS tbl, conname, pg_get_constraintdef(oid) AS def "
        "FROM pg_constraint WHERE contype = 'c' AND connamespace = 'public'::regnamespace",
    )
    by_name = {(row["tbl"], row["conname"]): row["def"] for row in rows}

    for table, column, enum_cls in columns:
        # One `CHECK` per column, not per class: a constraint cannot span tables,
        # so a shared vocabulary carries one on each column it guards. The name is
        # matched from the class's set rather than assumed to be its only member.
        candidates = {n for n in TEXT_CHECK_ENUMS[enum_cls] if (table, n) in by_name}
        expected_name = next(iter(candidates), f"ck_{table}_{column}_is_known")
        definition = by_name.get((table, expected_name))

        assert definition is not None, (
            f"{table}.{column} converted to text with no {expected_name} constraint; "
            f"the column is unguarded against every writer that is not Pydantic. "
            f"CHECK constraints on {table}: "
            f"{sorted(name for tbl, name in by_name if tbl == table)}"
        )

        # `col IN (...)` renders as `col = ANY (ARRAY['a'::text, ...])`.
        in_database = set(re.findall(r"'([^']*)'::text", definition))
        expected = {member.value for member in enum_cls}
        assert in_database == expected, (
            f"{table}.{column} and {enum_cls.__name__} disagree: "
            f"only in Python {expected - in_database}, "
            f"only in the constraint {in_database - expected}"
        )

    # Every registered name is real. Without this the loop above could fall back
    # to a derived name and pass while `TEXT_CHECK_ENUMS` named a constraint that
    # does not exist — a registry nobody checks is the thing this file is for.
    registered = {name for names in TEXT_CHECK_ENUMS.values() for name in names}
    present = {name for _, name in by_name}
    assert not registered - present, (
        f"TEXT_CHECK_ENUMS names constraints absent from the database: "
        f"{sorted(registered - present)}"
    )


def test_every_partial_index_predicate_survives_a_conversion(migrated_database: str) -> None:
    """The gap between "the index exists" and "the index is right".

    ``alembic check`` compares indexes by presence, so a partial index dropped
    for a type change and never recreated **is** caught — measured, not assumed:
    removing the recreate from step 3 failed two tests. What it does not compare
    is the ``WHERE`` clause, and `project-conventions` says so. An index dropped
    and recreated with the wrong predicate is therefore silent, and steps 3, 6
    and 8 drop and recreate seven of them between them.

    That failure is quiet in the worst way. ``ix_sessions_mentor_upcoming``
    rebuilt as ``WHERE status = 'completed'`` still exists, still has its name,
    still gets used — and simply never matches an upcoming session, so the query
    it exists to serve goes to a sequential scan and the rows it should have
    ranked go missing from a listing.

    Compares the literal values, not the rendered SQL: the cast is *expected* to
    change, from ``'pending_review'::lookup_status`` to ``'pending_review'::text``.
    The values inside it are what must not.
    """
    declared: dict[str, set[str]] = {}
    for table in Base.metadata.tables.values():
        for index in table.indexes:
            where = index.dialect_options.get("postgresql", {}).get("where")
            if where is None:
                continue
            declared[str(index.name)] = set(re.findall(r"'([^']*)'", str(where)))

        # **`ExcludeConstraint` is not an `Index`**, and missing that would have
        # left the most important object in the whole conversion unchecked.
        # `sessions_no_mentor_double_booking` lives in `table.constraints`, is
        # backed by an index PostgreSQL reports in `pg_indexes`, and carries the
        # `LIVE_STATUSES` predicate that step 8 had to transcribe. Walking only
        # `table.indexes` covered the three partial indexes beside it and not the
        # constraint they sit next to.
        for constraint in table.constraints:
            where = getattr(constraint, "where", None)
            if where is None or constraint.name is None:
                continue
            declared[str(constraint.name)] = set(re.findall(r"'([^']*)'", str(where)))

    partial = {name: values for name, values in declared.items() if values}
    assert partial, "no partial index declares a literal; this test would inspect nothing"

    rows = query(
        migrated_database,
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'",
    )
    in_database = {row["indexname"]: row["indexdef"] for row in rows}

    divergent: dict[str, str] = {}
    for name, values in partial.items():
        definition = in_database.get(name)
        if definition is None:
            divergent[name] = "absent from the database"
            continue
        _, _, clause = definition.partition(" WHERE ")
        found = set(re.findall(r"'([^']*)'", clause))
        if found != values:
            divergent[name] = f"model declares {sorted(values)}, database has {sorted(found)}"

    assert not divergent, f"partial index predicates disagree with the models: {divergent}"


def test_the_database_agrees_with_how_each_vocabulary_is_declared(migrated_database: str) -> None:
    """Catches a conversion applied to the model but not to the schema, or back.

    The two halves of a step land in one commit — the migration and the registry
    move — and nothing else checks they agree. A model switched to ``str_enum``
    with no migration leaves a live PostgreSQL enum the code believes is text;
    the reverse leaves a text column still registered as a type. Both pass
    ``alembic check``, because it compares a ``TypeDecorator`` by its ``impl``
    and sees ``TEXT`` either way.
    """
    rows = query(
        migrated_database,
        "SELECT table_name, column_name, data_type, udt_name "
        "FROM information_schema.columns WHERE table_schema = 'public'",
    )
    actual = {(row["table_name"], row["column_name"]): row for row in rows}

    converted = converted_columns()
    assert converted, "no converted columns; this test would inspect nothing"

    for table, column, enum_cls in converted:
        row = actual[(table, column)]
        assert row["data_type"] == "text", (
            f"{table}.{column} is declared str_enum({enum_cls.__name__}) but the "
            f"database has it as {row['udt_name']} — the migration did not run, "
            f"or ran without the model change"
        )

    # The mirror half — "a column declared `pg_enum` must be a live type" — is
    # gone with `pg_enum` itself. `test_no_postgresql_enum_type_survives` covers
    # what it protected, from the database side.


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


SERVER_DEFAULTS = """
SELECT c.relname AS tbl, a.attname AS col, pg_get_expr(d.adbin, d.adrelid) AS server_default
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
WHERE c.relkind = 'r' AND c.relname <> 'alembic_version'
"""


def test_every_declared_server_default_matches_the_database(migrated_database: str) -> None:
    """A fourth thing ``alembic check`` cannot see, and the one this file's own
    docstring opens with.

    ``compare_metadata`` does not diff server defaults — the docstring above
    names ``ALTER TABLE users ALTER COLUMN id SET DEFAULT uuid_generate_v7()`` as
    silent — so a model declaring ``server_default=text("120")`` against a
    database holding ``1440`` stays green everywhere. Nothing breaks until
    somebody autogenerates a migration from the drifted model and it emits a
    change nobody asked for.

    **Found by a mutation, not by reading.** Reverting the notice column's model
    default while leaving the migration alone survived the entire suite.

    Compared as *substrings* rather than for equality: PostgreSQL renders a
    default with its type, so ``text("1440")`` arrives as ``1440`` and
    ``text("'google_meet'")`` as ``'google_meet'::meeting_provider``. The
    declared form appearing inside the rendered one is the strongest claim that
    survives both.
    """
    rendered = {
        (row["tbl"], row["col"]): row["server_default"]
        for row in query(migrated_database, SERVER_DEFAULTS)
    }

    drifted: list[str] = []
    checked = 0
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if column.server_default is None:
                continue
            declared = str(getattr(column.server_default, "arg", "")).strip()
            if not declared:
                continue
            actual = rendered.get((table.name, column.name))
            checked += 1
            if actual is None:
                drifted.append(
                    f"{table.name}.{column.name}: declared {declared!r}, none in database"
                )
            elif declared.strip("'") not in actual:
                drifted.append(
                    f"{table.name}.{column.name}: declared {declared!r}, database {actual!r}"
                )

    assert checked, "no server defaults inspected; this test would prove nothing"
    assert not drifted, "model server defaults disagree with the database: " + "; ".join(drifted)


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
    """ADR 0015, and **this test is the decision** — the record only explains it.

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
        # M4. Both were primary keys in the package — a composite one on
        # participants, and `session_type_id` itself on the booking config — and
        # ADR 0015 demoted both. Without these, two attendance rows for one
        # person on one session, and two booking configs for one session type,
        # both become legal and nothing surfaces it.
        ("session_participants", "(session_id, user_id)"),
        ("session_type_booking_configs", "(session_type_id)"),
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


# --------------------------------------------------------------------------
# ADR 0013 as a path property
# --------------------------------------------------------------------------

# Tables that answer "who did what, and when". A user row must never be
# deletable in a way that takes one of these with it — not directly, and not
# through a chain of individually-defensible cascades.
#
# **This list grows with every phase**, and adding to it is the deliberate act
# that keeps the check meaningful. M2 brings `user_awards`; M4 brings `sessions`,
# `session_events` and `reviews`; M5 brings `credit_transactions`, whose
# retention is a legal obligation rather than audit hygiene.
#
# M4 adds the first two of its three. `reviews` is not in this phase, so it joins
# the set with the migration that creates it rather than being named here ahead
# of a table that does not exist — the assertion above rejects a name with no
# matching table precisely so this list cannot drift into fiction.
#
# `session_participants` is deliberately *not* retained. It cascades from
# `sessions`, which is — so nothing reaches it that does not first have to get
# through a retained table. Attendance is part of the session record rather than
# an independent claim about a person.
RETAINED_ON_USER_DELETE = frozenset(
    {
        "admin_users",
        "user_legal_consents",
        "sessions",
        "session_events",
        # The ledger is financial evidence (ADR 0013, M5b decision 7). A
        # ledger whose rows vanish with the account cannot answer "I was
        # charged for a session that never ran", which is the first of D8's
        # four reasons for it existing.
        "credit_lots",
        "credit_transactions",
    }
)

CASCADE_EDGES = """
SELECT con.conrelid::regclass::text AS child,
       con.confrelid::regclass::text AS parent
FROM pg_constraint con
WHERE con.contype = 'f'
  AND con.confdeltype = 'c'
  AND con.connamespace = 'public'::regnamespace
"""


def test_no_cascade_path_reaches_a_table_that_must_be_retained(migrated_database: str) -> None:
    """ADR 0013 reasons one edge at a time; ``DELETE`` follows the whole chain.

    The record's rule — cascade where the child is meaningless without its
    parent, restrict where it is audit or legal evidence — is applied correctly
    across every foreign key in M1. It is still not sufficient, because deletion
    follows the **transitive closure** of cascade edges rather than one hop.

    The failure it cannot see looks like this. M4 adds ``sessions.mentee_id ON
    DELETE CASCADE``, which is defensible: a session belongs to its mentee. Then
    it adds ``session_notes.session_id ON DELETE CASCADE``, also defensible: a
    note belongs to its session. Neither reviewer is wrong, both edges pass, and
    ``DELETE FROM users`` now destroys session history that the package's own
    anonymisation plan says must be **RETAINED**.

    So the property is asserted over paths, not edges. It passes today — nothing
    reaches a retained table — and it goes red on the commit that adds the second
    hop, which is the moment somebody can still choose differently. ADR 0013's
    Confirmation section names "nothing checks that a future migration applies
    this rule" as its own weakest point; this is that check.
    """
    rows = query(migrated_database, CASCADE_EDGES)

    children: dict[str, list[str]] = {}
    for row in rows:
        children.setdefault(row["parent"], []).append(row["child"])

    present = {r["tbl"] for r in query(migrated_database, PRIMARY_KEYS)}
    missing = RETAINED_ON_USER_DELETE - present
    assert not missing, (
        f"RETAINED_ON_USER_DELETE names tables that do not exist: {sorted(missing)}. "
        f"A typo here disables the check silently."
    )

    # Breadth-first over cascade edges only, recording how each table was reached
    # so a failure names the actual chain rather than just its endpoint.
    reached: dict[str, list[str]] = {"users": ["users"]}
    queue = ["users"]
    while queue:
        parent = queue.pop(0)
        for child in children.get(parent, []):
            if child not in reached:
                reached[child] = [*reached[parent], child]
                queue.append(child)

    violations = {
        table: " -> ".join(path)
        for table, path in reached.items()
        if table in RETAINED_ON_USER_DELETE
    }
    # Worded to avoid naming a statement: ruff's S608 pattern-matches the literal
    # text of a delete against a table name and flags this message as an
    # injection vector, in a string that is never executed. Rewording is honest;
    # a per-line suppression here would be one more suppressed rule nobody reads.
    assert not violations, (
        f"deleting a user would cascade into a table that must be retained: {violations}"
    )
