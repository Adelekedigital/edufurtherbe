"""The intake form: what a mentor asks, and what a mentee answered.

Four tables, landing together because half of them is worse than none — a
question definition with nowhere to store an answer is a schema asserting a
feature nobody can use.

    session_type_questions          what this offering asks
    session_type_question_options   the choices, for a multi-choice question
    intake_submissions              one per booking, the mentee's form
    intake_answers                  one per question answered
    intake_files                    a file a mentee uploaded to answer with

**Its own module rather than more of `sessions.py`.** That module holds five
models and is past the ~500-line tripwire settled decision #54 sets; nine would
be absurd. The split is by subject, as #54 requires and not by size: *the intake
form* is a subject, with its own lifecycle and its own reader.

**Follows `docs/edufurther-migration/schema/04_sessions.sql` rather than
diverging from it**, so there is no ADR here. Five things differ and every one is
a standing rule this schema already applies everywhere:

* the two vocabularies are `text` + `CHECK`, not PostgreSQL enums (#100)
* index names take `ix_`, not the package's `idx_`
* the `updated_at` trigger is attached per table, never by the package's blanket
  `attach_updated_at_triggers()` scanner (#23)
* a foreign key with no action in the package takes an explicit one here, chosen
  by ADR 0013 — cascade where the child is meaningless alone, restrict where it
  is evidence
* every table already carries the surrogate `id` ADR 0015 requires, so nothing
  to reconcile
"""

import datetime
import uuid

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import IntakeFileType, IntakeStatus, QuestionType
from app.infra.db.base import Base, TimestampMixin
from app.infra.db.types import check_is_known, str_enum


class SessionTypeQuestion(TimestampMixin, Base):
    """One question an offering asks before the session.

    **`CASCADE` from the offering, which is the one place a cascade is right in
    this stack.** A question has no meaning apart from the offering that asks it
    and records no fact about anybody — ADR 0013's test exactly. In practice it
    never fires: session types are soft-deleted, and no foreign key sees an
    `UPDATE`.

    **`deleted_at`, so a question can be retired without taking its answers.**
    `intake_answers.question_id` restricts, so a mentor removing a question from
    their form would otherwise be refused by every answer ever given to it. Soft
    deletion is what lets the form change while the record of what was asked
    survives.
    """

    __tablename__ = "session_type_questions"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    session_type_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("session_types.id", ondelete="CASCADE"), nullable=False
    )

    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    question_type: Mapped[QuestionType] = mapped_column(str_enum(QuestionType), nullable=False)
    is_required: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    display_order: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))
    #: For `multi_choice` only: whether several options may be picked. False is
    #: single choice. One choice type plus this flag, rather than a second
    #: vocabulary value, because the canonical package declares one choice type
    #: (ADR 0007) and "one or several" is a property of it, not a new kind (#200).
    allows_multiple: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))

    #: Who added it. `RESTRICT` rather than the package's unspecified action:
    #: ADR 0013 makes an authorship record evidence, and a user delete that
    #: silently rewrote it to null would lose the only attribution there is.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    __table_args__ = (
        # The only read: this offering's live questions, in order. Partial, and
        # `alembic check` cannot compare the predicate, so a test asserts it.
        Index(
            "ix_session_type_questions_form",
            "session_type_id",
            "display_order",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        CheckConstraint(
            check_is_known("question_type", QuestionType),
            name="question_type_is_known",
        ),
        CheckConstraint(
            "NOT allows_multiple OR question_type = 'multi_choice'",
            name="only_choice_allows_multiple",
        ),
    )


class SessionTypeQuestionOption(TimestampMixin, Base):
    """One choice offered by a multi-choice question.

    **Written by the question endpoints since #200**: a mentor gives a
    `multi_choice` question its options, 2 to 10, in `sort_order`.

    **No `deleted_at`, deliberately.** An option removed from a question is
    referenced by `intake_answers.selected_option_id`, which restricts — so the
    row cannot go while an answer names it, and there is nothing a soft delete
    would add. The question above needs one for the opposite reason: its answers
    are what make it undeletable.
    """

    __tablename__ = "session_type_question_options"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    #: Named by hand. The convention renders
    #: `fk_<table>_<column>_<referred table>`, which here is 67 characters —
    #: past PostgreSQL's 63-byte limit, where SQLAlchemy silently truncates and
    #: appends a hash. `test_no_declared_identifier_exceeds_the_postgresql_limit`
    #: caught it, which is the failure that test was written for.
    question_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "session_type_questions.id",
            ondelete="CASCADE",
            name="fk_session_type_question_options_question_id",
        ),
        nullable=False,
    )
    option_text: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))

    __table_args__ = (
        Index("ix_session_type_question_options_question", "question_id", "sort_order"),
    )


class IntakeSubmission(TimestampMixin, Base):
    """One mentee's form for one booking.

    **`UNIQUE (session_id)` is the whole shape.** A session has one form, so this
    is a 1:1 extension — and per ADR 0015 the key is a surrogate `id` with the
    invariant re-declared as `UNIQUE`, exactly as `mentor_profiles.user_id` and
    `session_type_booking_configs.session_type_id` already are.

    **`mentee_id` is stored rather than joined through the session**, which looks
    like denormalisation and is not: it is what makes "every form this mentee
    submitted" a query on this table, and the session's own `mentee_id` is the
    cross-check rather than the source. It restricts, because a submitted form is
    evidence of what was asked and answered.
    """

    __tablename__ = "intake_submissions"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    mentee_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[IntakeStatus] = mapped_column(
        str_enum(IntakeStatus), nullable=False, server_default=text("'draft'")
    )
    #: Null while the form is a draft. Not derived from `status`: the two answer
    #: different questions — *where is this form* and *when did it arrive* — and a
    #: form moved to `reviewed` keeps the moment it was submitted.
    submitted_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    __table_args__ = (
        UniqueConstraint("session_id"),
        CheckConstraint(check_is_known("status", IntakeStatus), name="status_is_known"),
    )


class IntakeAnswer(TimestampMixin, Base):
    """One answer, in exactly one of three forms.

    **`exactly_one_answer_form` is the constraint that carries the design.** A
    `file_upload` answer must not also carry text, and a `multi_choice` one must
    not carry both an option and prose — the summing form refuses zero as well as
    two, which a chain of `OR`s would not.

    **What it deliberately does not check is that the form matches the
    question's type.** That spans two tables, so a `CHECK` cannot reach it and a
    trigger would be the only mechanism — the same choice `delete_session_type`
    faces and declines. The boundary enforces it instead, which is where a
    mismatch is a `422` naming the field.

    **`question_id` restricts.** An answer is evidence of what was asked, and a
    question deleted out from under it would leave prose nobody can interpret.
    That is why the question carries `deleted_at`.
    """

    __tablename__ = "intake_answers"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    submission_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("intake_submissions.id", ondelete="CASCADE"), nullable=False
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("session_type_questions.id", ondelete="RESTRICT"), nullable=False
    )

    answer_text: Mapped[str | None] = mapped_column(Text)
    #: The object path in Supabase Storage, never a URL — a stored URL would be
    #: a bearer link that outlives whatever produced it. **A foreign key to
    #: `intake_files.storage_key`**, so the answer and the file it names cannot
    #: disagree about which object that is; the key stays after retention
    #: removes the object, and `intake_files.deleted_at` says it has gone.
    file_storage_key: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey(
            "intake_files.storage_key",
            ondelete="RESTRICT",
            name="fk_intake_answers_file_storage_key",
        ),
    )
    #: Named by hand for the same reason as the option's own key: the rendered
    #: convention is 66 characters.
    selected_option_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "session_type_question_options.id",
            ondelete="RESTRICT",
            name="fk_intake_answers_selected_option_id",
        ),
    )

    answered_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        # Named by hand: the `uq` convention renders on `column_0_name` alone,
        # so this and any future pair starting at `submission_id` would collide
        # on one name — the defect `base.py` warns about and
        # `mentor_conferencing_options` already hit.
        # **One row per chosen option for a multiple-choice answer** (#207): the
        # CHECK below allows one `selected_option_id` per row, so several options
        # are several rows. What the old `UNIQUE (submission_id, question_id)`
        # protected survives as two partial indexes — one text or file answer
        # per question, and each option chosen once. Partial, so `alembic check`
        # cannot compare the predicates; a test asserts them.
        Index(
            "ux_intake_answers_one_per_question",
            "submission_id",
            "question_id",
            unique=True,
            postgresql_where=text("selected_option_id IS NULL"),
        ),
        Index(
            "ux_intake_answers_one_per_option",
            "submission_id",
            "question_id",
            "selected_option_id",
            unique=True,
            postgresql_where=text("selected_option_id IS NOT NULL"),
        ),
        Index("ix_intake_answers_submission", "submission_id"),
        CheckConstraint(
            "(answer_text IS NOT NULL)::int "
            "+ (file_storage_key IS NOT NULL)::int "
            "+ (selected_option_id IS NOT NULL)::int = 1",
            name="exactly_one_answer_form",
        ),
    )


class IntakeFile(TimestampMixin, Base):
    """A file a mentee uploaded, before and after it answers a question.

    **Uploaded first, linked at booking.** `POST /me/intake-files` writes this
    row with `session_id` null; the booking that answers with it sets
    `session_id` in its own transaction, and only a row that is still unlinked
    and still the caller's can be linked — which is what stops one upload
    answering two bookings, or somebody else's.

    **`created_at` is the upload time**, so retention counts from it (settled
    decision on intake files). A second `uploaded_at` would hold the same
    instant under another name.

    **Never hard-deleted once linked.** Retention removes the *object* and sets
    `deleted_at`; the row stays, because an answer's `file_storage_key` names it
    and the answer is evidence of what the mentee sent. `purged_at` records that
    the object is confirmed gone, so the sweep stops retrying it. An upload
    nobody linked is deleted outright once its object is.

    **`session_id` is `SET NULL`**, not cascade: were a session ever removed,
    its file becomes an unlinked upload and the sweep deletes the object, where
    a cascade would drop the row and leave the object behind with nothing
    pointing at it.
    """

    __tablename__ = "intake_files"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    uploader_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("sessions.id", ondelete="SET NULL")
    )
    #: The object path inside the private intake bucket: the uploader's id and
    #: a fresh random id (`storage_key` in `domain/intake_files.py`), never the
    #: upload's name — so no two rows share an object, and a retention delete
    #: can only ever reach this row's.
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    #: The name to offer on download, sanitised on the way in.
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_type: Mapped[IntakeFileType] = mapped_column(str_enum(IntakeFileType), nullable=False)
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    purged_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    __table_args__ = (
        UniqueConstraint("storage_key"),
        Index("ix_intake_files_uploader", "uploader_id"),
        Index("ix_intake_files_session", "session_id"),
        # What the sweep asks every run: live files, oldest first.
        Index(
            "ix_intake_files_live_created",
            "created_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        CheckConstraint(
            check_is_known("content_type", IntakeFileType), name="content_type_is_known"
        ),
        CheckConstraint("size_bytes > 0", name="size_is_positive"),
        CheckConstraint("char_length(filename) BETWEEN 1 AND 255", name="filename_length"),
        CheckConstraint("purged_at IS NULL OR deleted_at IS NOT NULL", name="purged_after_deleted"),
    )
