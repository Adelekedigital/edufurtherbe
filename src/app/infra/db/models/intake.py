"""The intake form: what a mentor asks, and what a mentee answered.

Four tables, landing together because half of them is worse than none — a
question definition with nowhere to store an answer is a schema asserting a
feature nobody can use.

    session_type_questions          what this offering asks
    session_type_question_options   the choices, for a multi-choice question
    intake_submissions              one per booking, the mentee's form
    intake_answers                  one per question answered

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
    CheckConstraint,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import IntakeStatus, QuestionType
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
    )


class SessionTypeQuestionOption(TimestampMixin, Base):
    """One choice offered by a multi-choice question.

    **Nothing writes these yet, and the table ships anyway.** The UI's intake
    screens use `free_text` and `file_upload`; `multi_choice` is the third value
    the canonical package declares, and dropping the table would be an
    undeclared divergence from a document ADR 0007 makes authoritative rather
    than a deferral under #21.

    What changed the arithmetic is #100's completion: the sub-rule against
    shipping an unused vocabulary value existed because `ALTER TYPE ... ADD
    VALUE` is permanent, and #107 recorded that it "does not survive its
    removal". Being early now costs a migration rather than forever.

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
    #: The object path in Supabase Storage, never a URL. Images and uploads are
    #: keyed on their own content (ADR 0019), and a stored URL would be a bearer
    #: link that outlives whatever produced it.
    file_storage_key: Mapped[str | None] = mapped_column(Text)
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
        UniqueConstraint(
            "submission_id", "question_id", name="uq_intake_answers_submission_id_question_id"
        ),
        Index("ix_intake_answers_submission", "submission_id"),
        CheckConstraint(
            "(answer_text IS NOT NULL)::int "
            "+ (file_storage_key IS NOT NULL)::int "
            "+ (selected_option_id IS NOT NULL)::int = 1",
            name="exactly_one_answer_form",
        ),
    )
