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

from app.domain.enums import (
    ActorType,
    AdminRole,
    ApprovalStatus,
    AttendanceStatus,
    AuthProvider,
    AvailabilityExceptionType,
    LanguageProficiency,
    LegalDocumentType,
    ListingStatus,
    LookupStatus,
    MeetingProvider,
    MentorStatusType,
    PrimaryRole,
    SessionReasonCode,
    SessionRole,
    SessionStatus,
    UnlistedReason,
    VerificationStatus,
)

# Every closed vocabulary that has a PostgreSQL type, and the name of that type.
#
# A registry rather than a per-column argument, because it is the only thing a
# test can iterate. `alembic check` is **blind to enum labels** — verified, not
# assumed: after `ALTER TYPE language_proficiency ADD VALUE 'expert'` on a
# migrated database it still reports "No new upgrade operations detected". So
# adding a member to a StrEnum and forgetting the migration passes ruff, mypy,
# the layer check, `alembic check` and the whole suite, then fails at runtime
# with `invalid input value for enum` on the first insert that uses it.
#
# `test_every_enum_type_matches_its_python_class` walks this mapping, and a
# second test asserts the mapping covers every StrEnum in `domain.enums`, so a
# new vocabulary cannot be omitted from the check by being forgotten here.
PG_ENUM_TYPES: dict[type[StrEnum], str] = {
    PrimaryRole: "primary_role",
    AdminRole: "admin_role",
    AuthProvider: "auth_provider",
    LanguageProficiency: "language_proficiency",
    LegalDocumentType: "legal_document_type",
    LookupStatus: "lookup_status",
    ApprovalStatus: "approval_status",
    ListingStatus: "listing_status",
    UnlistedReason: "unlisted_reason",
    MentorStatusType: "mentor_status_type",
    VerificationStatus: "verification_status",
    MeetingProvider: "meeting_provider",
    AvailabilityExceptionType: "availability_exception_type",
    SessionStatus: "session_status",
    SessionRole: "session_role",
    AttendanceStatus: "attendance_status",
    SessionReasonCode: "session_reason_code",
    ActorType: "actor_type",
}


def pg_enum[E: StrEnum](enum_cls: type[E]) -> ENUM:
    """A PostgreSQL enum whose labels are the member *values*.

    The type name comes from ``PG_ENUM_TYPES`` rather than from the class name:
    it is a schema identifier that must match
    `docs/edufurther-migration/schema/00_foundation.sql` exactly, and deriving it
    would couple it to a Python class name that is free to change.

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
        name=PG_ENUM_TYPES[enum_cls],
        create_type=False,
        values_callable=_values,
    )


def _values(enum_cls: type[StrEnum]) -> Sequence[str]:
    return [member.value for member in enum_cls]
