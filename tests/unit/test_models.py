"""Guarantees that must hold for every model, checked by inspection.

``persistence-patterns`` requires the timestamp rule be enforced "with a test
that inspects every model, not with the mixin" — a mixin can be forgotten on the
next model; a red suite cannot.

The column half is checked here. The other half — that the database trigger is
actually attached, so ``updated_at`` moves — needs a database and lives in
``tests/integration/test_reference_data.py``. Having the columns and having the
trigger are different facts, and only one of them is visible from the model.
"""

from sqlalchemy import TIMESTAMP

from app.infra.db import models
from app.infra.db.base import Base

# Every model the project is expected to define. Update deliberately, in the same
# change that adds a model — this is what turns "somebody forgot to import it"
# from a silently smaller test run into a failure.
EXPECTED_MODELS = {"Country", "Language"}

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
