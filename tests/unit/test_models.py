"""Guarantees that must hold for every model, checked by inspection.

``persistence-patterns`` requires the timestamp rule be enforced "with a test
that inspects every model, not with the mixin" — a mixin can be forgotten on the
next model; a red suite cannot.

The column half is checked here. The other half — that the database trigger is
actually attached, so ``updated_at`` moves — needs a database and lives in
``tests/integration/test_reference_data.py``. Having the columns and having the
trigger are different facts, and only one of them is visible from the model.
"""

from enum import StrEnum

from sqlalchemy import TIMESTAMP

from app.domain import enums
from app.infra.db import models
from app.infra.db.base import Base
from app.infra.db.types import PG_ENUM_TYPES

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
}

TIMESTAMP_COLUMNS = ("created_at", "updated_at")


def mapped_classes() -> list[type]:
    return [mapper.class_ for mapper in Base.registry.mappers]


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
        missing = [name for name in TIMESTAMP_COLUMNS if name not in columns]
        assert not missing, f"{mapper.class_.__name__} is missing {missing}"


def test_timestamps_are_timezone_aware() -> None:
    """``timestamptz``, never ``timestamp``.

    PostgreSQL stores UTC either way, but the naive type discards the offset on
    write. Across Lagos, Toronto and Berlin that is silent data loss, not a
    formatting preference.
    """
    for mapper in Base.registry.mappers:
        for name in TIMESTAMP_COLUMNS:
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
        column = mapper.columns["updated_at"]

        assert column.onupdate is None, f"{mapper.class_.__name__}.updated_at has an ORM onupdate"
        assert column.server_onupdate is None, f"{mapper.class_.__name__}.updated_at"


def test_the_users_id_has_no_default_of_any_kind() -> None:
    """ADR 0009 §9 names this assertion, and it is the only thing that holds it.

    ``users.id`` *is* the Supabase auth user id. A default — ORM-side or
    server-side — would quietly mint a valid-looking id for any insert that
    forgot to pass the real one, producing a row that can never be logged into
    and that looks entirely normal in every query, dashboard and export.

    Every other table in the schema takes ``uuid_generate_v7()``, so restoring
    this one "for consistency" is a natural-looking change with no visible
    consequence until a user cannot sign in.
    """
    column = models.User.__table__.columns["id"]

    assert column.default is None, "users.id has an ORM-side default"
    assert column.server_default is None, "users.id has a server default"


def test_every_other_table_does_generate_its_own_id() -> None:
    """The counterweight to the test above.

    Asserting only that ``users.id`` has no default would be satisfied by a
    schema in which nothing generates ids at all — every insert then depends on
    the caller remembering, which is the failure mode ``users`` accepts
    deliberately and no other table should.
    """
    generated = {
        mapper.class_.__name__
        for mapper in Base.registry.mappers
        if "id" in mapper.columns and mapper.columns["id"].server_default is not None
    }

    assert generated == {"AdminUser", "AuthIdentity", "LegalDocument", "UserLegalConsent"}


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
