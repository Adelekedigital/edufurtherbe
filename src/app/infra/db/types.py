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
# `test_every_enum_type_matches_its_python_class` walks this mapping, and
# `test_every_domain_enum_is_registered_exactly_once` asserts the three registries
# below partition every StrEnum in `domain.enums`, so a new vocabulary cannot be
# omitted from the check by being forgotten here — nor can a converted one be
# left in two places while the conversion is half done.
PG_ENUM_TYPES: dict[type[StrEnum], str] = {
    PrimaryRole: "primary_role",
    AdminRole: "admin_role",
    AuthProvider: "auth_provider",
    LanguageProficiency: "language_proficiency",
    LegalDocumentType: "legal_document_type",
    LookupStatus: "lookup_status",
    ApprovalStatus: "approval_status",
    ListingStatus: "listing_status",
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

# Vocabularies already converted per settled decision #100 — a `text` column and
# a `CHECK` — mapped to the name of the constraint that guards them.
#
# Empty until the first column converts, and deliberately so: this ships with the
# `unlisted_reason` step, whose whole job is to prove the harness before any data
# moves. The parity test that walks this mapping arrives with the first entry,
# because a test iterating an empty registry inspects nothing and reports green —
# the failure shape `test_schema_parity.py` exists to prevent.
TEXT_CHECK_ENUMS: dict[type[StrEnum], str] = {}

# Vocabularies with no single database column to constrain, and why.
#
# **Not a waiting room.** A member here is a decision that the database cannot
# hold this vocabulary, recorded so nobody re-derives it — the same reason
# `APPEND_ONLY` and `RETAINED_ON_USER_DELETE` are written down rather than
# inferred.
UNCONSTRAINED_ENUMS: dict[type[StrEnum], str] = {
    # `mentor_status_events.reason` is not a closed set. It carries free text on
    # a decline, an `UnlistedReason` value on a self-pause, and free admin text
    # on an unlisting — `DeclineRequest.reason` reaches it through
    # `set_listing`, up to 1000 characters of whatever an admin typed. A `CHECK`
    # naming these four values would reject the admin path, and one conditioned
    # on `status_type = 'unlisted'` would reject it too, because that is the very
    # event the free text lands on.
    #
    # So the enum is a set of **sentinels** the application writes and reads back
    # by equality (`may_self_resume` compares against `MENTOR_PAUSED`), not a
    # vocabulary the column is restricted to. Constraining it needs the
    # `reason_code` / `reason_text` split `SessionReasonCode` already documents,
    # which is a schema change and a separate decision.
    UnlistedReason: "mentor_status_events.reason is free text carrying a sentinel",
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
