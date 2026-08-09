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

from app.domain.enums import ApprovalStatus, ListingStatus, MeetingProvider, UnlistedReason
from app.infra.db.base import Base, TimestampMixin
from app.infra.db.types import pg_enum


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
        pg_enum(ApprovalStatus), nullable=False, server_default=text("'pending'")
    )
    approved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="RESTRICT", name="fk_mentor_profiles_approved_by_users"),
    )
    decline_reason: Mapped[str | None] = mapped_column(Text)

    # The other half of the decision. `approved_by` had no counterpart, so a
    # decline recorded *that* it happened and never *who* — and unlike a count,
    # that cannot be reconstructed afterwards. Reusing `approved_by` for both
    # would put a decliner in a column named for approval, which is the shape
    # `usage_count` and the old `updated_at` both failed in.
    #
    # A `mentor_status_events` log is the right end state and is not this: one
    # transition needs two columns, a third one needs the table.
    declined_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    declined_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="RESTRICT", name="fk_mentor_profiles_declined_by_users"),
    )

    listing_status: Mapped[ListingStatus] = mapped_column(
        pg_enum(ListingStatus), nullable=False, server_default=text("'unlisted'")
    )
    # Defaults to `never_approved`, which is right for a new signup and wrong for
    # every migrated mentor — the unlisted ones in the extract are already
    # approved. The transform sets it explicitly rather than inheriting this.
    unlisted_reason: Mapped[UnlistedReason | None] = mapped_column(
        pg_enum(UnlistedReason), server_default=text("'never_approved'")
    )
    unlisted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    headline: Mapped[str | None] = mapped_column(Text)
    years_of_experience: Mapped[int | None] = mapped_column()

    # DEFAULT false, where the package has true. Legacy stored a blank on 10 of
    # 15 mentors, and blank meant "never turned it on" — so false is what the
    # data says, and a migrated mentor and a new one should not start on
    # opposite settings. The exposure is bounded: a mentor is only bookable when
    # approved AND listed, both of which they opted into, and in M3 a mentor with
    # no availability has no bookable slots regardless.
    #
    # This is the mentor's *general* setting. Per-session-type configs override
    # it in M4, resolving as COALESCE(session_type.x, mentor.x).
    requires_booking_confirmation: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )

    default_meeting_venue: Mapped[MeetingProvider] = mapped_column(
        pg_enum(MeetingProvider), nullable=False, server_default=text("'google_meet'")
    )
    # Null on every migrated row. Legacy stored a link even when the venue was
    # not custom — residue, because selecting custom auto-created a per-session
    # link that lived on the session record. The CHECK is what stops that residue
    # being carried forward as a static personal room.
    custom_meeting_url: Mapped[str | None] = mapped_column(Text)

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
        Index(
            "ix_mentor_profiles_unlisted",
            "unlisted_reason",
            "unlisted_at",
            postgresql_where=text("listing_status = 'unlisted'"),
        ),
        # **One direction only, and that is forced rather than lax.** A custom
        # URL requires the custom venue; the custom venue does **not** require a
        # URL. The symmetric version —
        # `(default_meeting_venue = 'custom') = (custom_meeting_url IS NOT NULL)`
        # — would reject rows this migration has to write: legacy "External
        # Video Tool" maps to `custom`, and its stored link is residue that we
        # deliberately drop, so those mentors arrive on `custom` with a null URL.
        #
        # The gap that leaves is real and belongs to M3, not to a constraint: a
        # mentor on `custom` with no URL has no link and no way to auto-create
        # one, so booking must refuse or fall back rather than produce a session
        # nobody can join. Recorded here so that is a decision rather than an
        # oversight.
        #
        # Bare name; the `ck` convention renders the `ck_mentor_profiles_` prefix.
        CheckConstraint(
            "custom_meeting_url IS NULL OR default_meeting_venue = 'custom'",
            name="custom_url_requires_custom_venue",
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
