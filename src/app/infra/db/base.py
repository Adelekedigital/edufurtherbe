"""The declarative base, the constraint naming convention, and the timestamp mixin.

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

from datetime import datetime

from sqlalchemy import TIMESTAMP, MetaData, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

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
    """Declarative base for every model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """``created_at`` and ``updated_at`` on every table, without exception.

    Both are ``timestamptz``. Never ``timestamp``: PostgreSQL stores UTC either
    way, but the naive type silently discards the offset on write, which breaks
    across Lagos, Toronto and Berlin.

    **There is deliberately no ``onupdate=`` here.** ``updated_at`` is maintained
    by the ``set_updated_at()`` database trigger, attached per table in the
    migration that creates it. SQLAlchemy's ``onupdate`` is ORM-side — it fires on
    a flush of a dirty object or a Core ``update()``, and a raw SQL ``UPDATE``
    leaves the column untouched. Two mechanisms for one column means the one that
    is wrong is whichever you were not looking at, so there is only the trigger.

    The mixin does not guarantee anything on its own; it can be forgotten on the
    next model. A test inspects every mapped class instead, and a second test
    asserts the trigger is actually attached to every table that has the column.
    """

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=text("now()"), nullable=False
    )
