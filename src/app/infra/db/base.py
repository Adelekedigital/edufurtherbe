"""The declarative base and the constraint naming convention.

Every model in the project inherits from ``Base``, and every constraint the
database ends up with is named by the convention below.

**Why the convention is not optional.** An unnamed constraint gets whatever name
the tooling emits at the moment the migration runs: older Alembic versions used
the database default (``<table>_pkey``), current ones apply the convention here
(``pk_<table>``). The same migration file therefore produces different schemas at
different times, and a later migration that hard-codes a name to rename it cannot
run against a fresh database — which means no new environment can be provisioned,
and nothing reveals that until somebody tries.

Naming them here is necessary and not sufficient: hand-written migrations still
spell PK, UNIQUE and index names out explicitly.
"""

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# ``column_0_label`` on ix and ``column_0_name`` elsewhere is the documented
# SQLAlchemy set, not a local invention. A second foreign key to the same table
# still needs an explicit name — the convention cannot disambiguate those.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for every model.

    No timestamp mixin here yet. It arrives with the first tables, alongside the
    test that inspects every model for ``created_at``/``updated_at`` — a mixin
    with no users and no test is unverified code, and a mixin is exactly the kind
    of thing that gets forgotten on the next model.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
