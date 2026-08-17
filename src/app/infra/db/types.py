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

from sqlalchemy import Dialect, Text, TypeDecorator
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
    LookupStatus: "lookup_status",
    ApprovalStatus: "approval_status",
    ListingStatus: "listing_status",
    MentorStatusType: "mentor_status_type",
    MeetingProvider: "meeting_provider",
    SessionStatus: "session_status",
    SessionRole: "session_role",
    AttendanceStatus: "attendance_status",
    SessionReasonCode: "session_reason_code",
    ActorType: "actor_type",
}

# Vocabularies already converted per settled decision #100 — a `text` column and
# a `CHECK` — mapped to the name of the constraint that guards them.
#
# The **rendered** constraint name, not the bare one the model passes. The `ck`
# naming convention expands `admin_role_is_known` into
# `ck_admin_users_admin_role_is_known`, and this is what
# `test_every_converted_enum_has_a_check_naming_its_values` looks up in
# `pg_constraint`. Passing the rendered name to `CheckConstraint` is the
# double-prefix defect `test_check_constraints_land_under_the_name_the_model_reports`
# exists to catch — the two representations are deliberate and cross-checked.
#
# Step 2 of 8: the single-column, single-table vocabularies. No index predicate
# names any of these, no function reads one, and only three carry a server
# default — which is why they went first and built the shape the rest copy.
TEXT_CHECK_ENUMS: dict[type[StrEnum], str] = {
    PrimaryRole: "ck_users_primary_role_is_known",
    AdminRole: "ck_admin_users_admin_role_is_known",
    AuthProvider: "ck_auth_identities_provider_is_known",
    LanguageProficiency: "ck_user_languages_proficiency_is_known",
    LegalDocumentType: "ck_legal_documents_type_is_known",
    VerificationStatus: "ck_user_awards_verification_status_is_known",
    AvailabilityExceptionType: "ck_availability_exceptions_type_is_known",
}

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


class StrEnumText[E: StrEnum](TypeDecorator[E]):
    """A ``text`` column that still hands Python the ``StrEnum`` member.

    **This exists because plain ``Text`` silently returns ``str``.** Settled
    decision #100 converts every vocabulary from a PostgreSQL enum to ``text`` +
    ``CHECK``, and ``pg_enum`` was doing more than creating the type: the
    dialect-level ``ENUM`` coerces each row back into the Python class on read.
    A bare ``Text`` column does not, so ``Mapped[ApprovalStatus]`` over ``Text``
    type-checks clean and yields a ``str`` at runtime.

    Nothing raises when that happens. ``==`` still works, because a ``StrEnum``
    member equals its value — so most code is unaffected and the defect hides.
    **``is`` does not**, and identity is what
    ``mentor_status_store.may_self_resume`` used:

        if row is None or row[0] is not ApprovalStatus.APPROVED:

    Converted without this decorator, that comparison is ``False`` for every
    mentor and a self-paused mentor can never resume. It fails closed, so it is
    not an exposure — it is simply a feature that stops working, with a green
    suite. Audited 2026-08-16: it was the only identity comparison in the
    codebase reading a database column; the other twenty-nine sit on transform
    dataclasses that hold real Python enums and are unaffected.

    **Why not ``sqlalchemy.Enum(..., native_enum=False)``**, which does this in
    one argument: it emits ``VARCHAR(n)`` sized to the longest member at
    definition time, so adding a longer value later becomes a column type change
    and a table rewrite — reintroducing exactly the rigidity #100 exists to
    escape — and it hands the ``CHECK`` to SQLAlchemy's rendering, when the point
    of #100 is that the constraint is dropped and re-added in one statement.

    ``process_result_value`` raises on a value the class does not have, rather
    than passing the string through. The ``CHECK`` should make that impossible;
    if it ever is not, a loud read beats a quiet one.
    """

    impl = Text
    cache_ok = True

    def __init__(self, enum_cls: type[E]) -> None:
        self.enum_cls = enum_cls
        super().__init__()

    # `_dialect` rather than `noqa: ARG002`. SQLAlchemy calls both hooks
    # positionally, so the name is ours to choose, and a suppressed rule is one
    # more thing nobody reads.
    def process_bind_param(self, value: E | None, _dialect: Dialect) -> str | None:
        """``.value`` explicitly, matching what the ETL writes by hand."""
        return None if value is None else self.enum_cls(value).value

    def process_result_value(self, value: str | None, _dialect: Dialect) -> E | None:
        return None if value is None else self.enum_cls(value)


def str_enum[E: StrEnum](enum_cls: type[E]) -> StrEnumText[E]:
    """The converted counterpart of :func:`pg_enum` — ``text``, not a type."""
    return StrEnumText(enum_cls)


def check_is_known(column: str, enum_cls: type[StrEnum]) -> str:
    """The ``CHECK`` body for a converted column, rendered from the class.

    **The value list is not written out per model, and that is rule 8.** Seven
    columns convert in one release and fourteen more follow; a hand-typed
    ``IN ('super_admin', 'mentor_approval', 'limited_access')`` beside each is
    the same vocabulary in a second place, which this project calls a defect
    rather than a style question. Rendering it means a member added to the
    ``StrEnum`` cannot disagree with the model.

    The migration still writes its own literal — no migration imports from
    ``app`` — and `test_every_converted_enum_has_a_check_naming_its_values`
    compares what actually landed in ``pg_constraint`` against the class, so a
    migration that drifts from the model is caught by the database rather than
    by review.
    """
    rendered = ", ".join(f"'{member.value}'" for member in enum_cls)
    return f"{column} IN ({rendered})"
