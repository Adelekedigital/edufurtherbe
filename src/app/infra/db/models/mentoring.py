"""Mentoring: the shared vocabulary both sides of a match select from.

One module for mentor and mentee tables, because the thing that joins them —
``service_offerings`` — belongs to neither. Splitting by role would leave it
homeless. ``mentor_profiles``, the two junctions and the mentee goal tables join
this module in the next pull request; if it passes roughly 500 lines or seven
models, mentor and mentee separate and this table moves to its own module
(settled decision #33).
"""

import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, CheckConstraint, ForeignKey, Index, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import (
    ApprovalStatus,
    ListingStatus,
    MentorStatusType,
)
from app.infra.db.base import Base, TimestampMixin
from app.infra.db.types import check_is_known, str_enum


class ServiceOffering(TimestampMixin, Base):
    """What a mentor helps with, and what a mentee needs — **the same six rows**.

    Both ``mentor_service_offerings`` and ``mentee_goal_needs`` point here, which
    is what turns "does this mentor do what this mentee needs" into a join
    (package D12).

    **D12's stated premise turned out to be wrong, and the correction matters
    more than the record does.** It says Bubble held two *separate* option sets
    with no mapping between them. It held **one**, used by both sides — but both
    columns store the display name as **text at the moment of selection** rather
    than a live reference, so they are snapshots taken at different times and at
    different depths of the same tree. The mentee side is six parent values; the
    mentor side is five parents plus five children and renames
    (``Statement of Purpose`` and ``Letter of Recommendation`` are children of
    Document Preparation, ``Visa Interview`` of Interview Preparation, and
    ``Document Review`` is an old name for Document Preparation). The decision
    D12 reached is right; its reason is not two vocabularies but **one recorded
    at inconsistent depth**.

    That is why the seed is the six parents, flat. All sixteen legacy values map
    onto them, and matching begins working immediately — today a mentor offering
    "Document Review" can never match a mentee needing "document preparation".

    **Closed by construction, and that is load-bearing.** No ``status``, no
    ``merged_into_id``, no ``usage_count``, no ``created_by``: users cannot add a
    row. This is the matching axis, and free text destroys it on contact — "SOP
    help", "Statement of Purpose" and "sop" become three rows matching nothing,
    while the join silently returns fewer results and nobody can tell why. The
    long tail is handled where it actually lives: ``institutions`` and
    ``scholarship_programs`` are both open, with the merge machinery to match.

    **Children are deliberately absent, and no ``parent_id`` is carried "for
    later".** ADR 0008 rejected exactly that shape — a column nothing populates
    is indistinguishable from a column somebody forgot to populate, and the first
    join written against it would be silently wrong on every row. Adding the
    hierarchy later is additive: a column plus rows, with existing junction rows
    still pointing at valid parents. What is *not* free is splitting or renaming
    a parent, since this table has no ``merged_into_id`` — so the six stay
    stable and all future specificity goes into children.
    """

    __tablename__ = "service_offerings"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    # NOT NULL, unlike `scholarship_programs.slug`: every row here is authored by
    # the product, so every row has a stable identifier from the moment it
    # exists. It is what the ETL's mapping dict targets and what a test asserts
    # on — never `display_name`, which product is free to re-word, and never the
    # id, which differs in every environment.
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)

    # application | test | funding | career. A display grouping, not a filter —
    # four of the six rows land in `application`, so it discriminates very
    # little. Seeded as the package specifies and worth nothing more than that.
    category: Mapped[str | None] = mapped_column(Text)

    sort_order: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))


class MentorProfile(TimestampMixin, Base):
    """What replaces the non-derived parts of legacy ``Mentor (front search)``.

    That table was a denormalised read cache built around Bubble's workflow-unit
    pricing, so **its shape carries no design intent** — fields sat together
    because one screen rendered them together. Everything derived is dropped:
    session counts, review counts and completion percentage are all computed.

    **``user_id`` is a unique column, not the primary key.** The package makes it
    the key; ADR 0015 makes every table's key a surrogate ``id``. The `UNIQUE` is
    what carries the 1:1 invariant the primary key used to carry, and losing it
    would make two profiles per mentor legal, silently.

    That `UNIQUE` is also load-bearing for something else: **mentor-only tables
    reference ``mentor_profiles.user_id``, never ``users.id``.** The values are
    identical, but the foreign key makes it structurally impossible to attach a
    mentor-only row to a mentee. Repointing it at ``users`` would compile,
    migrate and pass every test while silently deleting that guarantee.

    **Approval and listing are separate, and the pair is not redundant.**
    Approval is a judgement made once; listing is whether the mentor currently
    wants to be found. Approved-and-paused is a real state that no single flag
    expresses. Profile-page *access* is deliberately not a column here — it is a
    rule about the viewer, because a mentee with a completed session must still
    reach the page or their history breaks and past reviews 404.
    """

    __tablename__ = "mentor_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    # Named by hand: this table has a second foreign key to `users`
    # (`approved_by`), and the convention renders on `column_0_name`.
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE", name="fk_mentor_profiles_user_id_users"),
        nullable=False,
        unique=True,
    )

    approval_status: Mapped[ApprovalStatus] = mapped_column(
        str_enum(ApprovalStatus), nullable=False, server_default=text("'pending'")
    )
    # `approved_at`, `approved_by`, `declined_at`, `declined_by` and
    # `decline_reason` are gone. Each described only the *most recent* decision,
    # so a mentor declined, re-applied and approved had lost two of three — and
    # each was a copy of the newest `mentor_status_events` row, which is the
    # drift non-negotiable #8 names.

    listing_status: Mapped[ListingStatus] = mapped_column(
        str_enum(ListingStatus), nullable=False, server_default=text("'unlisted'")
    )
    # Defaults to `never_approved`, which is right for a new signup and wrong for
    # every migrated mentor — the unlisted ones in the extract are already
    # approved. The transform sets it explicitly rather than inheriting this.
    # `unlisted_reason` and `unlisted_at` went the same way. The reason a
    # listing changed is a fact about a transition, not about the row.

    headline: Mapped[str | None] = mapped_column(Text)
    years_of_experience: Mapped[int | None] = mapped_column()

    #: Whether a booking against this mentor waits for them to accept it.
    #:
    #: ``DEFAULT false``, where the package has ``true``. The argument is settled
    #: decision #57 and is not restated here (#51).
    #:
    #: **This is the authority, and a config may override it.** D88 moved this
    #: column onto ``session_type_booking_configs``; the move is reversed because
    #: the mechanism was wrong rather than the destination — one fallback
    #: covering three fields, and all three failed differently. What makes a
    #: nullable override work here and not there: a mentor row always exists, so
    #: the chain cannot bottom out at nothing, which is precisely what killed the
    #: venue cascade (#102).
    #:
    #: ``NOT NULL``. Null on the *config* means inherit; null here would mean
    #: nothing at all.
    requires_booking_confirmation: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )

    # `primary_session_type_id` is gone, and with it
    # `trg_refuse_retiring_a_primary_offering`. D88 gave it two jobs: the
    # offering a mentee lands on, and the source an unconfigured offering fell
    # back to. The fallback lost its last member when
    # `requires_booking_confirmation` came back to this table, and the first job
    # was never real — nothing landed a mentee on it and nothing ordered by it.
    # `_live_session_types` orders by `name`, unique per mentor among live rows
    # and therefore total.

    # Display and filter convenience only. The full history is on
    # `education_entries` — a mentor with degrees from Nigeria, then the UK, then
    # Canada was previously flattened to one value, which is exactly what "who
    # studied in Canada" needs to search across.
    primary_study_country_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("countries.id", ondelete="RESTRICT")
    )
    primary_study_program: Mapped[str | None] = mapped_column(Text)

    deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    legacy_bubble_id: Mapped[str | None] = mapped_column(Text, unique=True)

    __table_args__ = (
        # These indexes plus the user_profiles ones ARE the search
        # implementation. No Typesense, no Meilisearch, no synced table.
        Index(
            "ix_mentor_profiles_searchable",
            "approval_status",
            "listing_status",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_mentor_profiles_study_country",
            "primary_study_country_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # `ix_mentor_profiles_unlisted` is gone rather than replaced. It indexed
        # `(unlisted_reason, unlisted_at)` to answer "who is unlisted, why, and
        # when" — now a query against `mentor_status_events`, served by that
        # table's own index. `ix_mentor_profiles_searchable` already covers
        # `listing_status` for the directory.
        #
        # Settled decision #100. Both columns are a **read model the database
        # maintains** (#73): application code never writes them, so the constraint
        # guards `apply_mentor_status` itself — the one writer — rather than a
        # caller. `ix_mentor_profiles_searchable` indexes both and names no value
        # in its predicate (`deleted_at IS NULL`), so it rebuilt with the table.
        CheckConstraint(
            check_is_known("approval_status", ApprovalStatus),
            name="approval_status_is_known",
        ),
        CheckConstraint(
            check_is_known("listing_status", ListingStatus),
            name="listing_status_is_known",
        ),
    )


class MentorServiceOffering(TimestampMixin, Base):
    """What a mentor helps with. Points at the same vocabulary a mentee selects.

    **``mentor_user_id`` references ``mentor_profiles(user_id)``, not
    ``users(id)``.** Same value, different guarantee: this way a row cannot be
    attached to a user who has no mentor profile. Changing the target to
    ``users`` is a one-word edit that keeps every test green and removes the only
    structural protection this table has.

    No ``legacy_bubble_id``: it is derived from a list column of the mentor
    record rather than from a Bubble Thing of its own (decision #27), so its
    idempotency key is the unique pair below.
    """

    __tablename__ = "mentor_service_offerings"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    mentor_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("mentor_profiles.user_id", ondelete="CASCADE"), nullable=False
    )
    # Named by hand, and shorter than the convention would produce. Left to the
    # convention this renders as
    # `fk_mentor_service_offerings_service_offering_id_service_offerings` — 65
    # characters, over PostgreSQL's 63-byte limit, which SQLAlchemy silently
    # truncates and hashes rather than rejecting. The database would then hold a
    # name that appears nowhere in this repository, and the first
    # `op.drop_constraint` written against the declared name would fail.
    # `test_no_declared_identifier_exceeds_the_postgresql_limit` is the guard.
    service_offering_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "service_offerings.id",
            ondelete="RESTRICT",
            name="fk_mentor_service_offerings_offering",
        ),
        nullable=False,
    )

    __table_args__ = (
        # The composite that used to be the primary key. Without it a mentor can
        # list the same offering twice, which the old key made impossible.
        Index(
            "ix_mentor_service_offerings_pair",
            "mentor_user_id",
            "service_offering_id",
            unique=True,
        ),
        Index("ix_mentor_service_offerings_offering", "service_offering_id"),
    )


class MenteeGoal(TimestampMixin, Base):
    """What a mentee is trying to do. 1:1 with the user.

    Legacy ``completedSession`` is dropped — derived from ``sessions``.

    ``degree_goal_raw`` keeps any legacy value that could not be mapped onto
    ``degree_levels`` rather than dropping it. Expect that to matter: 720
    production rows held "Masters", "masters", "MSc" and "Master's Degree" as
    free text, and no filter worked.
    """

    __tablename__ = "mentee_goals"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    # UNIQUE carries the 1:1 invariant the package's primary key carried, and is
    # also what the two goal junctions below reference.
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    degree_goal_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("degree_levels.id", ondelete="RESTRICT")
    )
    degree_goal_raw: Mapped[str | None] = mapped_column(Text)
    target_start_term: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)

    legacy_bubble_id: Mapped[str | None] = mapped_column(Text, unique=True)


class MenteeGoalCountry(TimestampMixin, Base):
    """Where a mentee wants to study. ``priority`` lets a first choice rank higher.

    ``user_id`` references ``mentee_goals(user_id)``, not ``users(id)`` — the
    same reasoning as ``MentorServiceOffering``. A target country belongs to a
    goal, so a user with no goals row cannot have one.
    """

    __tablename__ = "mentee_goal_countries"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("mentee_goals.user_id", ondelete="CASCADE"), nullable=False
    )
    country_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("countries.id", ondelete="RESTRICT"), nullable=False
    )
    priority: Mapped[int] = mapped_column(nullable=False, server_default=text("1"))

    __table_args__ = (
        Index("ix_mentee_goal_countries_pair", "user_id", "country_id", unique=True),
        Index("ix_mentee_goal_countries_country", "country_id"),
    )


class MenteeGoalNeed(TimestampMixin, Base):
    """What a mentee needs help with — **the same vocabulary mentors offer**.

    This is the join that makes basic matching work without any AI:

        SELECT m.mentor_user_id, COUNT(*) AS overlap
        FROM mentee_goal_needs g
        JOIN mentor_service_offerings m USING (service_offering_id)
        WHERE g.user_id = :mentee
        GROUP BY 1 ORDER BY overlap DESC;

    It only works because both sides were collapsed to the same six parent
    values. Legacy stored the mentor side at mixed depth, so "Document Review"
    and "document preparation" were different strings for one concept and the
    overlap was always zero.
    """

    __tablename__ = "mentee_goal_needs"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("mentee_goals.user_id", ondelete="CASCADE"), nullable=False
    )
    service_offering_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("service_offerings.id", ondelete="RESTRICT"), nullable=False
    )

    __table_args__ = (
        Index("ix_mentee_goal_needs_pair", "user_id", "service_offering_id", unique=True),
        Index("ix_mentee_goal_needs_offering", "service_offering_id"),
    )


class MentorStatusEvent(Base):
    """Every change to a mentor's approval or listing, append-only.

    **The write path.** Application code inserts here and never touches
    `mentor_profiles.approval_status` or `.listing_status`;
    `trg_apply_mentor_status` projects each event onto the column it concerns.
    That is structural rather than conventional on purpose — a helper everybody
    remembers to call is what this repository has been burned by four times,
    starting with `deleted_at IS NULL` typed into five statements and missed on
    the fifth.

    `alembic check` cannot see triggers, so the test inserts an event **directly**
    and asserts the column moved. Nothing else distinguishes a working trigger
    from a caller that happened to write both — and the first version of that
    trigger did not work at all, because PostgreSQL refuses a direct cast between
    two enum types.

    ``mentor_user_id`` references ``mentor_profiles(user_id)`` rather than
    ``users(id)``, the decision ``mentor_service_offerings`` already records:
    same value, different guarantee, and an event cannot attach to a user with no
    mentor profile.
    """

    __tablename__ = "mentor_status_events"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    mentor_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("mentor_profiles.user_id", ondelete="RESTRICT"),
        nullable=False,
    )
    status_type: Mapped[MentorStatusType] = mapped_column(
        str_enum(MentorStatusType), nullable=False
    )
    #: Free text on a decline, and on an unlisting **either** an `UnlistedReason`
    #: value or free text — `pause` writes `mentor_paused` and `decide` writes
    #: `never_approved`, while an admin unlisting through `set_listing` writes
    #: whatever `DeclineRequest.reason` carried.
    #:
    #: **So this column is not a closed set and takes no CHECK**, which is why
    #: `UnlistedReason` sits in `UNCONSTRAINED_ENUMS` rather than converting under
    #: settled decision #100. Constraining it means splitting `reason_code` from
    #: `reason_text`, the shape `SessionReasonCode` already documents. Readers
    #: compare by equality (`may_self_resume`), so a free-text reason simply does
    #: not match a sentinel — which is the intended behaviour, not a near miss.
    reason: Mapped[str | None] = mapped_column(Text)
    #: Who acted. Null only for rows the backfill wrote, where nobody did.
    #: Named for the house convention — `created_by`, `granted_by`, `approved_by`.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )

    # **No `TimestampMixin`, and no `updated_at`.** This table is append-only: a
    # row states what happened at a moment, and a fact that can be edited is not
    # a log. `updated_at` on it would be a column nothing could ever move, which
    # is the same emptiness `usage_count` was deleted for.
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        # The only read is one mentor's history, newest first.
        Index("ix_mentor_status_events_mentor", "mentor_user_id", text("created_at DESC")),
        # Settled decision #100. This is the value `apply_mentor_status` branches
        # on, so an unknown one would reach the ELSE arm and be written to
        # `listing_status` — a silent mis-projection rather than an error. The
        # constraint refuses it at the insert instead.
        CheckConstraint(
            check_is_known("status_type", MentorStatusType),
            name="status_type_is_known",
        ),
    )
