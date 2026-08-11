"""Guarantees that must hold for every model, checked by inspection.

``persistence-patterns`` requires the timestamp rule be enforced "with a test
that inspects every model, not with the mixin" — a mixin can be forgotten on the
next model; a red suite cannot.

The column half is checked here. The other half — that the database trigger is
actually attached, so ``updated_at`` moves — needs a database and lives in
``tests/integration/test_reference_data.py``. Having the columns and having the
trigger are different facts, and only one of them is visible from the model.
"""

import ast
from enum import StrEnum

from sqlalchemy import TIMESTAMP

from app.domain import enums
from app.infra.db import models
from app.infra.db.base import Base
from app.infra.db.models.sessions import LIVE_STATUSES
from app.infra.db.types import PG_ENUM_TYPES
from conftest import PROJECT_ROOT

# Every model the project is expected to define. Update deliberately, in the same
# change that adds a model — this is what turns "somebody forgot to import it"
# from a silently smaller test run into a failure.
EXPECTED_MODELS = {
    "Country",
    "Language",
    # M1 identity
    "User",
    "UserProfile",
    "AuthIdentity",
    "UserOnboarding",
    "UserLanguage",
    "AdminUser",
    "LegalDocument",
    "UserLegalConsent",
    # M2 lookups. `institutions` and `scholarship_programs` are open — users
    # create rows and an admin curates them; `degree_levels` and
    # `service_offerings` are closed vocabularies the product defines.
    "Institution",
    "DegreeLevel",
    "ServiceOffering",
    "ScholarshipProgram",
    # M2 profiles. `user_scholarship_experience` is deliberately absent — the
    # legacy field behind it has no option set and no values, so there is
    # nothing to migrate and nothing to write it.
    "MentorProfile",
    "MentorServiceOffering",
    "MentorStatusEvent",
    "EducationEntry",
    "UserAward",
    "MenteeGoal",
    "MenteeGoalCountry",
    "MenteeGoalNeed",
    # M3 availability. `CalendarConnection` is deliberately absent: nothing in
    # M3 reads or writes it, ADR 0012 has not settled the OAuth arrangement its
    # columns encode, and settled decision #21 ships a table with the phase that
    # first needs it.
    "AvailabilityRule",
    "AvailabilityException",
    # M4 sessions. Five of the package's nine tables in `04_sessions.sql`:
    # `SessionTypeQuestion`, `SessionTypeQuestionOption`, `IntakeSubmission`,
    # `IntakeAnswer` and `SessionNote` are deliberately absent. None has a legacy
    # source or a read surface, and settled decision #21 ships a table with the
    # phase that first needs it — the intake stack is a unit and arrives whole.
    "SessionType",
    "SessionTypeBookingConfig",
    "Session",
    "SessionParticipant",
    "SessionEvent",
}

TIMESTAMP_COLUMNS = ("created_at", "updated_at")

#: Append-only tables, which carry `created_at` and no `updated_at`.
#:
#: **Named rather than silently absent.** A row here states what happened at a
#: moment; a fact that can be edited is not a log, so `updated_at` would be a
#: column nothing could ever move — the same emptiness `usage_count` was deleted
#: for. Listing the exemption makes it a decision somebody made, and a new model
#: that quietly drops `updated_at` still fails.
APPEND_ONLY = frozenset({"MentorStatusEvent", "SessionEvent"})


def mapped_classes() -> list[type]:
    return [mapper.class_ for mapper in Base.registry.mappers]


# PostgreSQL's NAMEDATALEN is 64, so an identifier may be 63 bytes.
POSTGRES_IDENTIFIER_LIMIT = 63


def test_no_declared_identifier_exceeds_the_postgresql_limit() -> None:
    """A name too long is **silently** truncated and hashed, not rejected.

    SQLAlchemy shortens any identifier over the dialect limit and appends a
    deterministic hash — no warning, no ``NOTICE``. ``op.f()`` does not exempt
    it: that marks a name as already conventioned, not as already short enough.

    Nothing breaks on the day it happens. The constraint exists and is enforced,
    and ``alembic check`` compares foreign keys by column signature rather than
    by name, so the whole gate stays green. It breaks later, when a migration
    calls ``op.drop_constraint`` with the name the source file shows and
    PostgreSQL answers *constraint does not exist* — and the file somebody reads
    to find the real name hands them one that was never created.

    Caught in review on the M2 profile tables, where a foreign key declared at
    65 characters landed as 60 with a hash. The margin is thinner than it looks:
    several other names in this schema sit at 58 and 59.

    Every kind of identifier is checked, not only the one that bit us. A
    constraint name is where the convention makes long names likely, but the
    limit applies to tables, columns and types identically, and a guard that
    covered one of four would invite exactly the "what about the others"
    question it exists to answer. Nothing is close today — the longest are 24,
    29 and 20 characters — which is the cheapest possible moment to fix the
    scope.
    """
    too_long: list[str] = []
    for table in Base.metadata.tables.values():
        too_long += [f"table {table.name}" if len(table.name) > POSTGRES_IDENTIFIER_LIMIT else ""]
        too_long += [
            f"column {table.name}.{c.name}"
            for c in table.c
            if len(c.name) > POSTGRES_IDENTIFIER_LIMIT
        ]
        names = [c.name for c in table.constraints if c.name] + [i.name for i in table.indexes]
        too_long += [str(n) for n in names if len(str(n)) > POSTGRES_IDENTIFIER_LIMIT]

    too_long += [
        f"enum type {name}"
        for name in PG_ENUM_TYPES.values()
        if len(name) > POSTGRES_IDENTIFIER_LIMIT
    ]
    too_long = [n for n in too_long if n]

    assert Base.metadata.tables, "no tables registered; this test would inspect nothing"
    assert PG_ENUM_TYPES, "no enum types registered; this test would inspect nothing"
    assert not too_long, (
        "these identifiers exceed PostgreSQL's 63-character limit and will be "
        f"silently truncated and hashed: {sorted(too_long)}"
    )


def test_the_expected_models_are_registered() -> None:
    """A model that is never imported is invisible to metadata and to autogenerate.

    Asserting the exact set, rather than a minimum, is what catches the reverse
    mistake too: a model deleted or renamed without anybody updating the tests
    that describe the schema.
    """
    assert {klass.__name__ for klass in mapped_classes()} == EXPECTED_MODELS
    assert set(models.__all__) == EXPECTED_MODELS


def test_every_model_carries_both_timestamp_columns() -> None:
    """The assertion that the registry is non-empty is not padding.

    Without it this test passes by iterating nothing — which is precisely how it
    would behave if the models package stopped exporting, and precisely the shape
    of failure this repository keeps meeting: a check that scans zero things and
    reports green.
    """
    mappers = list(Base.registry.mappers)

    assert mappers, "no models registered; this test would otherwise inspect nothing and pass"

    for mapper in mappers:
        columns = set(mapper.columns.keys())
        expected = ("created_at",) if mapper.class_.__name__ in APPEND_ONLY else TIMESTAMP_COLUMNS
        missing = [name for name in expected if name not in columns]
        assert not missing, f"{mapper.class_.__name__} is missing {missing}"

        if mapper.class_.__name__ in APPEND_ONLY:
            assert "updated_at" not in columns, (
                f"{mapper.class_.__name__} is declared append-only and carries updated_at"
            )


def test_timestamps_are_timezone_aware() -> None:
    """``timestamptz``, never ``timestamp``.

    PostgreSQL stores UTC either way, but the naive type discards the offset on
    write. Across Lagos, Toronto and Berlin that is silent data loss, not a
    formatting preference.
    """
    for mapper in Base.registry.mappers:
        expected = ("created_at",) if mapper.class_.__name__ in APPEND_ONLY else TIMESTAMP_COLUMNS
        for name in expected:
            column = mapper.columns[name]

            assert isinstance(column.type, TIMESTAMP), f"{mapper.class_.__name__}.{name}"
            assert column.type.timezone is True, f"{mapper.class_.__name__}.{name} is naive"


def test_no_model_declares_an_orm_side_onupdate() -> None:
    """``updated_at`` is maintained by the database trigger, and only by it.

    ``onupdate`` fires on an ORM flush and not on a raw SQL ``UPDATE``, so a model
    carrying both would give two answers depending on how the row was written —
    and the wrong one would be whichever nobody was looking at.
    """
    for mapper in Base.registry.mappers:
        if mapper.class_.__name__ in APPEND_ONLY:
            continue
        column = mapper.columns["updated_at"]

        assert column.onupdate is None, f"{mapper.class_.__name__}.updated_at has an ORM onupdate"
        assert column.server_onupdate is None, f"{mapper.class_.__name__}.updated_at"


def test_every_model_has_a_generated_surrogate_id() -> None:
    """ADR 0015, on the model side. **No exceptions, and that is the point.**

    ``persistence-patterns`` already required this, and it was overridden twice
    anyway — once for ISO lookups keyed on their code, once for 1:1 extensions
    keyed on ``user_id`` — with every gate green through both. A rule enforced by
    prose is re-decided by whoever reads it next, so it is asserted here over the
    whole registry rather than described.

    The database half lives in ``tests/integration/test_schema_parity.py``, which
    also catches a composite or natural key. This half catches the model drifting
    from it, which is the direction ``alembic check`` cannot see.
    """
    mappers = list(Base.registry.mappers)

    assert mappers, "no models registered; this test would otherwise inspect nothing"

    for mapper in mappers:
        name = mapper.class_.__name__
        table = mapper.class_.__table__

        assert "id" in table.c, f"{name} has no id column"

        pk = list(table.primary_key.columns)
        assert [c.name for c in pk] == ["id"], (
            f"{name} has primary key {[c.name for c in pk]}, not a sole 'id'"
        )

        default = table.c["id"].server_default
        assert default is not None, f"{name}.id has no server default"
        assert "uuid_generate_v7" in str(default.arg), (
            f"{name}.id is generated by {default.arg}, not uuid_generate_v7()"
        )


def test_the_supabase_auth_id_is_a_column_not_the_key() -> None:
    """ADR 0014, superseding ADR 0009 §9.

    A provider's identifier as our primary key would put a Supabase-issued value
    in every foreign key of all sixty-six tables — collapsing two of the three
    identifier spaces tier 2 says must never be interchangeable. It is one
    nullable column instead, and nullable is load-bearing: a migrated user exists
    before they have ever authenticated, which is what lets M1c load without
    calling Supabase at all.
    """
    auth_id = models.User.__table__.c["auth_id"]

    assert auth_id.nullable, "auth_id must be nullable — a migrated user has not logged in yet"
    assert auth_id.unique, "auth_id must be unique — it identifies one Supabase account"
    assert not auth_id.primary_key, "auth_id is a vendor identifier and must never be the key"


def test_every_domain_enum_has_a_postgresql_type() -> None:
    """``PG_ENUM_TYPES`` is what the label-parity test iterates.

    An enum missing from it is not checked against the database at all, and the
    omission is invisible — the parity test simply inspects one fewer type and
    still reports green. That is the "check that scans zero things" shape this
    repository keeps meeting, so the registry's completeness is asserted rather
    than assumed.
    """
    declared = {
        obj
        for obj in vars(enums).values()
        if isinstance(obj, type) and issubclass(obj, StrEnum) and obj is not StrEnum
    }

    assert declared, "no enums found; this test would otherwise pass by inspecting nothing"
    assert declared == set(PG_ENUM_TYPES), (
        f"not registered in PG_ENUM_TYPES: {declared - set(PG_ENUM_TYPES)}"
    )


def test_postgresql_type_names_are_unique() -> None:
    """Two classes mapped to one type name would make the parity test compare
    one of them against the other's labels and pass for the wrong reason."""
    names = list(PG_ENUM_TYPES.values())

    assert len(names) == len(set(names)), f"duplicate type names in PG_ENUM_TYPES: {names}"


def migration_constants(name: str) -> dict[str, str]:
    """Every migration defining a module-level string constant ``name``.

    Read with ``ast`` rather than imported. A migration is not a package, and
    importing one executes its module body and pulls in alembic's ``op`` — a
    parse is enough to read a literal and cannot have side effects.
    """
    found: dict[str, str] = {}
    for path in sorted((PROJECT_ROOT / "migrations" / "versions").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if name in targets and isinstance(node.value, ast.Constant):
                found[path.name] = str(node.value.value)
    return found


def test_the_live_status_predicate_has_one_meaning() -> None:
    """``LIVE_STATUSES`` is written in three places and must say one thing.

    **The copies cannot be removed, and that is not laziness.** No migration in
    this chain imports from ``app``, deliberately: a migration is a historical
    artefact, and importing a live constant would let a later edit silently
    change what an old revision does. So decision #43's other remedy applies —
    pin the copies with a test that fails when they diverge, exactly as
    ``EXPORT_TIMEZONE`` is pinned.

    This matters more than a tidy-up. The predicate decides which sessions the
    double-booking constraint guards. If the model and the migration ever
    disagree, the partial indexes cover one set of rows and the constraint
    another — and nothing else in the gate compares them, because a predicate
    inside a ``text()`` string is not a symbol any linter can bind.

    Found by **searching** the migrations rather than listing them, so a fourth
    copy is covered the day it is written.
    """
    copies = migration_constants("LIVE_STATUSES")

    assert copies, (
        "no migration defines LIVE_STATUSES; this test would otherwise pass by "
        "comparing nothing, which is the failure it exists to prevent"
    )
    divergent = {path: value for path, value in copies.items() if value != LIVE_STATUSES}
    assert not divergent, f"LIVE_STATUSES disagrees with the model's {LIVE_STATUSES!r}: {divergent}"
