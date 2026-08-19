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

import pytest
from sqlalchemy import TIMESTAMP

from app.domain import enums
from app.infra.db import models
from app.infra.db.base import Base
from app.infra.db.models.sessions import LIVE_STATUSES
from app.infra.db.types import TEXT_CHECK_ENUMS, UNCONSTRAINED_ENUMS, StrEnumText
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
    # The fourth `mentor_*` table, which is why it lives in `mentoring.py` rather
    # than beside `session_types`. It fires #54's seven-model tripwire; the split
    # of mentor from mentee is deferred deliberately — see the model's docstring.
    "MentorConferencingOption",
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
    # Per-offering windows, which **replace** general availability rather than
    # intersecting them. Same module as the rules they replace, and the same
    # shape, so `bookable()` needs no second code path.
    "SessionTypeSchedulingWindow",
    # M4 sessions. Eight of the package's nine tables in `04_sessions.sql`.
    # `SessionNote` is the one still absent: it has no legacy source and no read
    # surface, and settled decision #21 ships a table with the phase that first
    # needs it. The intake stack was absent for the same reason and has since
    # arrived, whole, because it is a unit.
    "SessionType",
    "SessionTypeBookingConfig",
    # The intake stack, in its own module: `sessions.py` is already five
    # models and past #54's line tripwire, and "the intake form" is a
    # subject of its own.
    "SessionTypeQuestion",
    "SessionTypeQuestionOption",
    "IntakeSubmission",
    "IntakeAnswer",
    "Session",
    "SessionParticipant",
    "SessionEvent",
    # Platform infrastructure, which serves every feature and belongs to none.
    # First of the three in `08_features_platform.sql`; `outbox_events` and
    # `feature_flags` ship with whatever first needs them (#21).
    "IdempotencyKey",
    # The second, and the one the package designed for analytics dispatch —
    # it carries notifications first because that is what has a producer.
    "OutboxEvent",
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

    # The CHECK that replaces a dropped type is an identifier under the same
    # limit, and `ck_<table>_<rule>` is where this schema's longest names live.
    # `UNCONSTRAINED_ENUMS` is not checked: its values are reasons, not names.
    too_long += [
        f"check constraint {name}"
        for names in TEXT_CHECK_ENUMS.values()
        for name in names
        if len(name) > POSTGRES_IDENTIFIER_LIMIT
    ]
    too_long = [n for n in too_long if n]

    assert Base.metadata.tables, "no tables registered; this test would inspect nothing"
    assert TEXT_CHECK_ENUMS, "no vocabularies registered; this test would inspect nothing"
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


def test_every_domain_enum_is_registered_exactly_once() -> None:
    """``TEXT_CHECK_ENUMS`` is what the constraint-parity test iterates.

    An enum missing from it is not checked against the database at all, and the
    omission is invisible — the parity test simply inspects one fewer type and
    still reports green. That is the "check that scans zero things" shape this
    repository keeps meeting, so the registry's completeness is asserted rather
    than assumed.

    **Three registries now, and the partition is the assertion.** Settled
    decision #100 moves each vocabulary from a PostgreSQL type to ``text`` +
    ``CHECK``, one migration at a time, so for most of that work the enums are
    split across two states with a third — ``UNCONSTRAINED_ENUMS`` — for the ones
    no single column can hold. Asserting only "every enum is somewhere" would let
    a vocabulary sit in both registries and report green. Settled decision #100
    is finished — every vocabulary is now converted or deliberately
    unconstrained — but the partition is what a *new* one has to satisfy, and it
    is the reason a new vocabulary cannot be added without deciding which it is.
    """
    declared = {
        obj
        for obj in vars(enums).values()
        if isinstance(obj, type) and issubclass(obj, StrEnum) and obj is not StrEnum
    }
    registries = {
        "TEXT_CHECK_ENUMS": set(TEXT_CHECK_ENUMS),
        "UNCONSTRAINED_ENUMS": set(UNCONSTRAINED_ENUMS),
    }

    assert declared, "no enums found; this test would otherwise pass by inspecting nothing"

    registered = set().union(*registries.values())
    assert declared == registered, (
        f"registered in none of {sorted(registries)}: {declared - registered}; "
        f"registered but not declared in domain.enums: {registered - declared}"
    )

    duplicated = {
        enum_cls.__name__: sorted(
            name for name, members in registries.items() if enum_cls in members
        )
        for enum_cls in declared
        if sum(enum_cls in members for members in registries.values()) > 1
    }
    assert not duplicated, (
        f"registered in more than one registry, so the conversion is half applied: {duplicated}"
    )


def test_postgresql_type_names_are_unique() -> None:
    """Two classes mapped to one type name would make the parity test compare
    one of them against the other's labels and pass for the wrong reason.

    ``TEXT_CHECK_ENUMS`` is included because it has the same hazard one layer
    over: two vocabularies naming one ``CHECK`` would have the constraint-parity
    test assert the same constraint twice and never notice the missing one.
    ``UNCONSTRAINED_ENUMS`` is excluded deliberately — its values are prose
    reasons, not identifiers, and nothing looks them up.
    """
    names = [n for ns in TEXT_CHECK_ENUMS.values() for n in ns]

    assert len(names) == len(set(names)), f"duplicate schema identifiers registered: {names}"


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


def test_the_unlisted_reason_labels_have_one_meaning() -> None:
    """The same pinning, for a copy that only a ``downgrade`` ever executes.

    ``c9d4e2a71f68`` drops the orphaned ``unlisted_reason`` type and its
    ``downgrade`` recreates it from a transcribed label list. That path runs on
    almost no day, which is exactly why it drifts: ``UnlistedReason`` can gain or
    lose a member and every gate stays green, because after the drop no
    PostgreSQL type mirrors the class and the label-parity test has nothing to
    compare.

    **Order is asserted, not just membership.** PostgreSQL sorts an enum by
    declaration order, so a rebuild from an alphabetised list produces a type
    that compares differently from the one that was dropped — and comparison is
    silent, not an error.
    """
    expected = ", ".join(f"'{member.value}'" for member in enums.UnlistedReason)
    copies = migration_constants("UNLISTED_REASON_LABELS")

    assert copies, (
        "no migration defines UNLISTED_REASON_LABELS; this test would otherwise "
        "pass by comparing nothing, which is the failure it exists to prevent"
    )
    divergent = {path: value for path, value in copies.items() if value != expected}
    assert not divergent, f"UnlistedReason declares {expected!r}: {divergent}"


def test_a_converted_column_hands_back_the_enum_member_not_a_string() -> None:
    """The whole reason ``StrEnumText`` exists rather than a bare ``Text``.

    A plain ``Text`` column returns ``str``. ``==`` still works, because a
    ``StrEnum`` member equals its value, so most code is unaffected and the
    defect hides — but ``is`` does not, and identity is what
    ``mentor_status_store.may_self_resume`` uses to decide whether a mentor may
    put themselves back on the list. Converted without the decorator, that
    comparison is ``False`` for every mentor: fails closed, so not an exposure,
    but a feature that silently stops working with a green suite.

    Asserted with ``is`` deliberately. ``==`` would pass against a raw string and
    prove nothing at all.
    """
    column = StrEnumText(enums.PrimaryRole)

    assert column.process_result_value("mentee", None) is enums.PrimaryRole.MENTEE
    assert column.process_result_value(None, None) is None
    assert column.process_bind_param(enums.PrimaryRole.MENTOR, None) == "mentor"
    assert column.process_bind_param(None, None) is None

    # A value the class does not have is a loud read, not a silent passthrough.
    # The CHECK should make this unreachable; if it ever is not, this is how it
    # surfaces.
    with pytest.raises(ValueError, match="not a valid PrimaryRole"):
        column.process_result_value("wizard", None)


def migration_tuples(name: str) -> dict[str, list[tuple[str, ...]]]:
    """Every migration defining a module-level tuple-of-tuples ``name``.

    ``migration_constants`` above reads a plain string and handles ``ast.Assign``
    only. A conversion table is annotated, so it parses as ``ast.AnnAssign``, and
    its rows are nested tuples — different enough to be its own reader rather
    than a flag on the first one.
    """
    found: dict[str, list[tuple[str, ...]]] = {}
    for path in sorted((PROJECT_ROOT / "migrations" / "versions").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            target = None
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                target = node.target.id
            elif isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                target = names[0] if names else None
            if target != name or not isinstance(node.value, ast.Tuple):
                continue
            found[path.name] = [
                tuple(str(el.value) for el in row.elts if isinstance(el, ast.Constant))
                for row in node.value.elts
                if isinstance(row, ast.Tuple)
            ]
    return found


def test_conferencing_providers_are_meeting_providers() -> None:
    """The two vocabularies overlap, and must not drift apart by accident.

    `ConferencingProvider` is what a mentor may **choose**; `MeetingProvider` is
    what a session **used**, and keeps `zoom` because a session that happened on
    a venue keeps naming it after the venue stops being selectable. So the first
    is a strict subset of the second, and the values are genuinely duplicated.

    Non-negotiable #8 admits exactly this shape — *extract, or pin the copies
    with a test that fails when they diverge* — and extraction is not available
    here: the sets differ, and a single enum could not express "selectable now"
    and "ever written" at once.

    **Strict subset, not equality.** Equality would fail the moment `zoom` is
    correct to keep on one side and absent from the other, which is today. What
    this catches is a value added to `ConferencingProvider` that
    `MeetingProvider` does not know — a venue a mentor can select and a session
    cannot record.
    """
    conferencing = {member.value for member in enums.ConferencingProvider}
    meeting = {member.value for member in enums.MeetingProvider}

    assert conferencing < meeting, (
        f"selectable providers are not a strict subset of recorded ones: "
        f"{conferencing - meeting} can be chosen but never written to "
        f"sessions.meeting_provider"
    )


def test_the_converted_enum_labels_have_one_meaning() -> None:
    """Pins each conversion migration's transcribed labels to its ``StrEnum``.

    The ``upgrade`` path is covered by the database:
    ``test_every_converted_enum_has_a_check_naming_its_values`` compares the live
    ``CHECK`` against the class. **The ``downgrade`` path is not.** It recreates
    the PostgreSQL type from a label list written into the migration, runs on
    almost no day, and asserts nothing about order — and PostgreSQL sorts an enum
    by declaration order, so a rebuild from an alphabetised list restores a type
    that compares differently from the one dropped.

    The constraint name is derived here as ``ck_{table}_{column}_is_known``
    rather than read from the migration, so the convention itself is pinned: a
    step that names one of seven differently fails here rather than in review.

    **Only the newest migration per column is compared, and that correction came
    from `withdrawn`.** The first version of this test compared *every*
    migration's labels to the current class, which held exactly as long as no
    vocabulary ever changed. Adding ``SessionStatus.WITHDRAWN`` made step 8's
    migration "disagree" with the class — correctly, because it is a historical
    artefact that must keep recording the seven values it wrote. Decision #43
    says a migration is history; a test demanding history match the present
    contradicts it, and would have forced either an edit to a shipped migration
    or a deleted test on the first vocabulary change after the conversion. Since
    filenames are date-prefixed, later in sorted order is later in the chain.
    """
    by_name = {name: cls for cls, names in TEXT_CHECK_ENUMS.items() for name in names}
    conversions = migration_tuples("CONVERSIONS")

    assert conversions, (
        "no migration defines CONVERSIONS; this test would otherwise pass by "
        "comparing nothing, which is the failure it exists to prevent"
    )

    # Last writer wins: the newest migration touching a column is the one whose
    # labels must equal the class today.
    authoritative: dict[tuple[str, str], tuple[str, str]] = {}
    for path in sorted(conversions):
        for table, column, _type_name, labels, *_ in conversions[path]:
            authoritative[(table, column)] = (path, labels)

    assert authoritative, "no columns found in any CONVERSIONS table"

    # A column the schema no longer has cannot be compared to a live class, and
    # demanding it be is the same mistake `withdrawn` corrected one step further
    # on. `session_type_booking_configs.meeting_venue` was converted by
    # `a7d2f4b8c051` and dropped by `d9e2b74c1f36`; its migration is history and
    # must keep naming the vocabulary it wrote.
    #
    # Keyed on the column being **absent from the models**, which is a fact about
    # the schema rather than an exemption list somebody maintains — so a
    # misspelled constraint on a column that still exists is not skipped.
    live_columns = {
        (table.name, column.name)
        for table in Base.metadata.tables.values()
        for column in table.columns
    }

    divergent: dict[str, str] = {}
    for (table, column), (path, labels) in authoritative.items():
        if (table, column) not in live_columns:
            continue
        constraint = f"ck_{table}_{column}_is_known"
        enum_cls = by_name.get(constraint)
        if enum_cls is None:
            divergent[f"{path}:{table}.{column}"] = f"no TEXT_CHECK_ENUMS entry named {constraint}"
            continue
        expected = ", ".join(f"'{member.value}'" for member in enum_cls)
        if labels != expected:
            divergent[f"{path}:{table}.{column}"] = f"{labels!r} != {expected!r}"

    assert not divergent, f"migration labels disagree with domain.enums: {divergent}"
