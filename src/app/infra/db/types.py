"""Column-type helpers shared by the models.

One function, and it exists to stop a specific silent defect.

**SQLAlchemy's ``Enum`` sends member *names* to the database, not values.**
Given ``PrimaryRole.MENTEE = "mentee"``, the default behaviour creates a
PostgreSQL type whose labels are ``MENTEE`` and ``MENTOR``. Nothing fails: the
migration applies, the ORM round-trips, and every test passes — because the ORM
is translating both ways and never shows you the label. It surfaces later, from
`psql`, from a reporting view, or from the ETL writing a literal, at which point
the labels disagree with `docs/edufurther-migration/` and with every hand-written
query anyone has authored against them.

``values_callable`` is the documented fix. Wrapping it here rather than repeating
the lambda per column is what makes it impossible to apply to four enums and
forget the fifth.
"""

from collections.abc import Sequence
from enum import StrEnum

from sqlalchemy.dialects.postgresql import ENUM


def pg_enum[E: StrEnum](enum_cls: type[E], name: str) -> ENUM:
    """A PostgreSQL enum whose labels are the member *values*.

    ``name`` is passed explicitly rather than derived from the class, because it
    is the database type name and it must match
    `docs/edufurther-migration/schema/00_foundation.sql` exactly — deriving it
    would couple a schema identifier to a Python class name that is free to
    change.

    ``create_type=False`` says SQLAlchemy must not emit ``CREATE TYPE`` of its
    own accord; the migration creates and drops every type by hand, so that the
    ``DROP TYPE`` statements in ``downgrade`` are visible in the file rather than
    implied. Without them ``DROP TABLE`` leaves the type behind and the next
    ``upgrade`` fails with "type already exists".

    **This is ``postgresql.ENUM``, not the generic ``sqlalchemy.Enum``, and the
    difference is not cosmetic.** Both compile the column to the native type, so
    the two look interchangeable. But ``create_type`` is a dialect-level
    parameter: the generic type accepts the keyword, silently discards it, and
    leaves no attribute behind — verified, not assumed. A no-op that reports
    nothing is the shape of defect this module exists to prevent, and it very
    nearly shipped inside the helper written to prevent it.
    """
    return ENUM(
        enum_cls,
        name=name,
        create_type=False,
        values_callable=_values,
    )


def _values(enum_cls: type[StrEnum]) -> Sequence[str]:
    return [member.value for member in enum_cls]
