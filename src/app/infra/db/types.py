"""How a closed vocabulary is stored, and the registries that keep it honest.

**Every one is a ``text`` column with a ``CHECK``** — settled decision #100,
completed across eight migrations ending with ``session_status``. This schema has
no PostgreSQL enum types left, and this module no longer offers a way to make
one.

Two functions and two registries:

``str_enum``      the column type — ``text`` in the database, the ``StrEnum``
                  member in Python
``check_is_known`` the ``CHECK`` body, rendered from the class so the vocabulary
                  is never typed out twice
``TEXT_CHECK_ENUMS`` every converted vocabulary and the constraints guarding it
``UNCONSTRAINED_ENUMS`` the ones no single column can hold, each with the reason

**Both defects this module has met were silent, and both shaped what is here.**
The first belonged to the enums it replaced: SQLAlchemy's ``Enum`` sends member
*names*, so ``PrimaryRole.MENTEE`` created a label ``MENTEE`` while the ORM
translated both ways and showed nobody — surfacing only from `psql` or from the
ETL writing a literal. The second belongs to the replacement: a bare ``Text``
column returns ``str``, so ``Mapped[ApprovalStatus]`` type-checks clean and every
``is`` comparison against a member becomes permanently false. ``str_enum``
exists for the second exactly as the deleted ``pg_enum`` existed for the first.

The pattern in both: the wrong thing works well enough that nothing reports it.
That is why the registries are iterated by tests rather than merely read.
"""

from enum import StrEnum

from sqlalchemy import Dialect, Text, TypeDecorator

from app.domain.enums import (
    ActorType,
    AdminRole,
    ApplicationStage,
    ApprovalStatus,
    AttendanceStatus,
    AuthProvider,
    AvailabilityExceptionType,
    ConferencingProvider,
    IntakeStatus,
    LanguageProficiency,
    LegalDocumentType,
    ListingStatus,
    LookupStatus,
    MeetingProvider,
    MentorStatusType,
    PrimaryRole,
    QuestionType,
    SessionReasonCode,
    SessionRole,
    SessionStatus,
    UnlistedReason,
    VerificationStatus,
)

# **`PG_ENUM_TYPES` and `pg_enum` are gone, and their absence is the point.**
#
# Settled decision #100 finished with `session_status` in step 8: this schema has
# no PostgreSQL enum types left, and a new vocabulary takes `text` + `CHECK`. A
# helper that still existed would be an invitation to use it, and #100 would go
# back to being enforced by prose — which this project has watched fail twice,
# with every gate green. Deleting it makes the rule structural: there is nothing
# to call.
#
# The reasoning that registry carried is preserved where it still applies.
# `alembic check` was blind to enum labels, and it is equally blind to `CHECK`
# constraints — `compare_metadata` does not diff them at all. So the parity tests
# did not retire with the enums; they moved to the constraints, and
# `test_every_converted_enum_has_a_check_naming_its_values` is the direct
# descendant of `test_every_enum_type_matches_its_python_class`.
#
# Vocabularies converted per #100 — a `text` column and
# a `CHECK` — mapped to the name of the constraint that guards them.
#
# **A set per class, because a vocabulary can guard more than one column.** Step
# 2 was seven classes on seven columns and a bare string sufficed; `lookup_status`
# in step 3 is one class on two tables, and `session_status` in step 8 is one on
# three. A `CHECK` cannot span tables, so each column carries its own — which is
# the one real cost of `text` + `CHECK` over a shared enum type, and the reason
# this maps to a set rather than a name.
#
# The names are the **rendered** form, not the bare one the model passes. The `ck`
# convention expands `admin_role_is_known` into
# `ck_admin_users_admin_role_is_known`, and this is what
# `test_every_converted_enum_has_a_check_naming_its_values` looks up in
# `pg_constraint`. Passing a rendered name to `CheckConstraint` is the
# double-prefix defect `test_check_constraints_land_under_the_name_the_model_reports`
# exists to catch — the two representations are deliberate and cross-checked.
TEXT_CHECK_ENUMS: dict[type[StrEnum], frozenset[str]] = {
    # Step 2 — single column, single table.
    PrimaryRole: frozenset({"ck_users_primary_role_is_known"}),
    AdminRole: frozenset({"ck_admin_users_admin_role_is_known"}),
    AuthProvider: frozenset({"ck_auth_identities_provider_is_known"}),
    LanguageProficiency: frozenset({"ck_user_languages_proficiency_is_known"}),
    LegalDocumentType: frozenset({"ck_legal_documents_type_is_known"}),
    VerificationStatus: frozenset({"ck_user_awards_verification_status_is_known"}),
    AvailabilityExceptionType: frozenset({"ck_availability_exceptions_type_is_known"}),
    # The first vocabulary this project designed rather than received, and
    # nullable — `application_stage IS NULL OR ...`, because a `CHECK` rejects
    # only what is false and an offering aimed at no particular stage is
    # ordinary.
    ApplicationStage: frozenset({"ck_session_types_application_stage_is_known"}),
    # Step 3 — the first shared vocabulary. Two tables whose rows users create
    # and an admin curates; `degree_levels` and `service_offerings` have no
    # status column because nobody can add a row to them.
    LookupStatus: frozenset(
        {
            "ck_institutions_status_is_known",
            "ck_scholarship_programs_status_is_known",
        }
    ),
    # Step 4 — the mentor status cluster. These three convert together because
    # `apply_mentor_status` reads `status_type` and writes the other two, and a
    # plpgsql body carries no dependency records: dropping one type while the
    # function still names it succeeds silently and fails at the next write.
    ApprovalStatus: frozenset({"ck_mentor_profiles_approval_status_is_known"}),
    ListingStatus: frozenset({"ck_mentor_profiles_listing_status_is_known"}),
    MentorStatusType: frozenset({"ck_mentor_status_events_status_type_is_known"}),
    # Step 5 — one column now, and nullable. `sessions.meeting_provider` allows
    # NULL and the `IN` constraint permits it without a special case: `NULL IN
    # (...)` is unknown, and a CHECK rejects only what is *false*.
    #
    # `ck_session_type_booking_configs_meeting_venue_is_known` was the second
    # entry and left with the column (`d9e2b74c1f36`). What a mentor may *choose*
    # is `ConferencingProvider` below; this enum is what a session *used*, and
    # keeps `zoom` for history even though nothing can select it.
    MeetingProvider: frozenset({"ck_sessions_meeting_provider_is_known"}),
    ConferencingProvider: frozenset({"ck_mentor_conferencing_options_provider_is_known"}),
    # Step 6 — `role` carries the first **unique** partial index to move.
    # The intake stack. `question_type` carries `multi_choice`, which nothing
    # writes yet — see the enum for why shipping it early is now cheap.
    QuestionType: frozenset({"ck_session_type_questions_question_type_is_known"}),
    IntakeStatus: frozenset({"ck_intake_submissions_status_is_known"}),
    # The second table to carry this vocabulary. `reviews.reviewed_for_role` is
    # the capacity a user was reviewed in, which `reviewed_for` cannot say on its
    # own and the migrated rows have no session to derive.
    SessionRole: frozenset(
        {
            "ck_session_participants_role_is_known",
            "ck_reviews_reviewed_for_role_is_known",
        }
    ),
    AttendanceStatus: frozenset({"ck_session_participants_attendance_status_is_known"}),
    # Step 7 — neither has an index dependency; `ix_session_events_reason` is
    # partial on `reason_code IS NOT NULL`, which names no enum literal.
    ActorType: frozenset({"ck_session_events_actor_type_is_known"}),
    SessionReasonCode: frozenset({"ck_session_events_reason_code_is_known"}),
    # Step 8 — the last one, and the widest: three columns across two tables,
    # three `CHECK`s, and four objects whose definitions named the type. Adding a
    # value here is now three constraint swaps in one migration rather than a
    # permanent `ALTER TYPE ... ADD VALUE`, which is what makes `WITHDRAWN`
    # cheap — see `docs/handoff-enum-to-text-check.md`.
    SessionStatus: frozenset(
        {
            "ck_sessions_status_is_known",
            "ck_session_events_from_status_is_known",
            "ck_session_events_to_status_is_known",
        }
    ),
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


class StrEnumText[E: StrEnum](TypeDecorator[E]):
    """A ``text`` column that still hands Python the ``StrEnum`` member.

    **This exists because plain ``Text`` silently returns ``str``.** Settled
    decision #100 converts every vocabulary from a PostgreSQL enum to ``text`` +
    ``CHECK``, and the ``pg_enum`` helper it replaced was doing more than
    creating the type: the
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
    """How every closed vocabulary is declared: ``text``, never a type."""
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
