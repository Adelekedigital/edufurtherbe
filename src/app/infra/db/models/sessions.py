"""Sessions: the booking itself, who attended, and every state it passed through.

Five tables, and one shape that governs them:

    sessions             the row a booking creates — WHO, WHEN, HOW LONG
    session_participants attendance, one row per person
    session_events       the immutable lifecycle log
    session_types        what a mentor offers
    session_type_booking_configs   how long that offering runs

**There is no ``bookings`` table.** Legacy split ``SessionBooking`` (1,073) from
``SessionTracker`` (935), and that was a Bubble workaround rather than a domain
distinction (package D3) — measured on the dev export, the two agree on mentor,
duration, cancellation, room and venue on all 103 linked pairs. A booking is the
*act* of claiming a slot; the row it creates is a session.

**The intake stack has since arrived, whole, in ``models/intake.py``** —
``session_type_questions``, ``session_type_question_options``,
``intake_submissions`` and ``intake_answers``. It was deferred out of this module
under settled decision #21 and landed as a unit, in a module of its own because
this one is already five models and past #54's line tripwire.

``session_notes`` is still absent, for the reason that deferred all five: no
legacy source and no read surface.

**Deletion policy, derived from ADR 0013 rather than copied from the package.**
The record's rule is cascade where the child is meaningless without its parent
and records no auditable fact; restrict where it is evidence. Applied here:

===============================  ==========  ====================================
Foreign key                      Rule        Why
===============================  ==========  ====================================
``booking_configs.type_id``      CASCADE     1:1 extension, no fact of its own
``participants.session_id``      CASCADE     attendance is part of the session
everything else                  RESTRICT    evidence, or points at evidence
===============================  ==========  ====================================

``session_types.mentor_user_id`` restricts where ``availability_rules`` cascades
against the same parent, and the difference is deliberate: a session type is
referenced by ``sessions``, which is retained. Cascading it would leave a
mentor-profile delete to be blocked by a *second* foreign key instead of by the
one a reader is looking at. ``mentor_status_events`` already restricts against
``mentor_profiles`` for the same class of reason.

**Sessions are never deleted and carry no ``deleted_at``.** A cancelled session
is still a session — still counted, still part of a mentor's history — so the
absence is the domain rule, not an oversight in the package.
"""

import datetime
import uuid

from sqlalchemy import (
    TIMESTAMP,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import (
    ActorType,
    ApplicationStage,
    AttendanceStatus,
    MeetingProvider,
    SessionReasonCode,
    SessionRole,
    SessionStatus,
)
from app.infra.db.base import Base, TimestampMixin
from app.infra.db.types import check_is_known, str_enum

#: The statuses a session is still *live* in — awaiting a decision, or agreed.
#:
#: Written once and reused by all three partial indexes, and by the exclusion
#: constraint that lands in the next pull request. Non-negotiable #8: this
#: predicate existing in a second place is a defect, and a predicate inside a
#: `text()` string is not a symbol any linter can bind — which is exactly how
#: `deleted_at IS NULL` reached five statements here and was missed on the fifth.
LIVE_STATUSES = "status IN ('pending_mentor_approval', 'confirmed')"


class SessionType(TimestampMixin, Base):
    """What a mentor offers. Availability says *when*; this says *what*.

    **Every migrated mentor receives an auto-created "General Mentorship" row**
    during the M4 load, so all legacy sessions can carry a ``session_type_id``
    and the column can become ``NOT NULL`` afterwards — one code path rather than
    a platform-default fallback forever (package D21).

    That auto-created row is where legacy ``CalendarSettings.meetingDuration-TxT``
    lands, per settled decision #58. The package drops mentor-level duration
    fifteen lines below the note that every mentor gets a session type, and the
    two lines were never connected: duration is a property of the *offering*, not
    a global preference, because a 15-minute question and a 60-minute document
    review share no meaningful default.
    """

    __tablename__ = "session_types"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    #: References `mentor_profiles(user_id)`, not `users(id)` — the same value
    #: with a different guarantee, following `mentor_status_events` and
    #: `availability_rules`. An offering belonging to a user with no mentor
    #: profile is not a state this schema should be able to represent.
    mentor_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("mentor_profiles.user_id", ondelete="RESTRICT"), nullable=False
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    #: What *kind* of help this offering is — a reference to the closed six-row
    #: `service_offerings` taxonomy, which is the axis a mentee's need and a
    #: mentor's offer already join on (#53). No new vocabulary.
    #:
    #: **Called `service_offering_id`, not `category`, and the rename is not
    #: cosmetic.** `service_offerings` has its own `category` column — a display
    #: grouping — so a foreign key named `category` would point at a table with a
    #: different `category` one join away. `mentor_service_offerings` and
    #: `mentee_goal_needs` already use this name.
    service_offering_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("service_offerings.id", ondelete="RESTRICT")
    )

    #: Which stage of an application this offering is aimed at. A closed set
    #: (#100), `text` + `CHECK`, with `ApplicationStage` at the boundary.
    application_stage: Mapped[ApplicationStage | None] = mapped_column(str_enum(ApplicationStage))
    #: `OTHER`'s label, and **only** `OTHER`'s — tied by the symmetric `CHECK`
    #: below. See `ApplicationStage` for why the escape hatch is kept.
    custom_stage_label: Mapped[str | None] = mapped_column(Text)

    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))

    #: Which of this mentor's conferencing options this offering is held on, or
    #: **null meaning use my default** — the same inherit-from-the-mentor shape
    #: `requires_booking_confirmation` uses.
    #:
    #: **The foreign key is composite**, declared in `__table_args__` below, and
    #: that is why the reference sits here rather than on the booking config. A
    #: single-column key is satisfied by *any* option row, including another
    #: mentor's; `(mentor_user_id, conferencing_option_id)` makes that
    #: unrepresentable. This table already carries `mentor_user_id`, so it costs
    #: nothing here and would cost a denormalised column or a trigger there.
    conferencing_option_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    __table_args__ = (
        # Named by hand: the convention renders on `column_0_name`, which would
        # give two different composite keys on this table the same prefix.
        ForeignKeyConstraint(
            ["mentor_user_id", "conferencing_option_id"],
            ["mentor_conferencing_options.user_id", "mentor_conferencing_options.id"],
            name="fk_session_types_conferencing_option",
            ondelete="RESTRICT",
            # `application_stage` and `custom_stage_label` are guarded below.
        ),
        # The only read: this mentor's live offerings. Partial, and `alembic
        # check` cannot compare the predicate, so a test asserts it.
        Index(
            "ix_session_types_mentor",
            "mentor_user_id",
            postgresql_where=text("is_active AND deleted_at IS NULL"),
        ),
        # **One live session type per mentor per name.**
        #
        # A product invariant first: a mentor holding two live types both called
        # "Document Review" is not a state worth being able to represent, and a
        # mentee choosing between them could not tell them apart.
        #
        # It is also the ETL's idempotency key, and that is why it lands now
        # rather than later. `session_types` is the only migrated table with no
        # `legacy_bubble_id` and no natural key, so a re-run — which is the
        # recovery plan, not the exception — would create a second type per
        # mentor. Delete-then-insert is not available either: `sessions.
        # session_type_id` references it with `RESTRICT`, so on the second run
        # the delete fails.
        #
        # Partial on `deleted_at IS NULL` so a retired type never blocks a new
        # one with the same name. **`ON CONFLICT` must repeat that predicate
        # verbatim** — omitting it raises `InvalidColumnReferenceError` rather
        # than silently choosing another index, which is measured in
        # `test_the_upsert_requires_the_index_predicate`.
        Index(
            "ix_session_types_mentor_name",
            "mentor_user_id",
            "name",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # `IS NULL OR` because the column is nullable and a `CHECK` rejects only
        # what is *false* — `NULL IN (...)` is unknown and would pass anyway, but
        # spelling it out is what stops the next reader adding a `NOT NULL` here
        # by inference.
        CheckConstraint(
            f"application_stage IS NULL OR {check_is_known('application_stage', ApplicationStage)}",
            name="application_stage_is_known",
        ),
        # **Symmetric.** `other` with no label renders a blank chip; a named
        # stage carrying a stale label is dead data that survives an edit. The
        # one-directional form is what let `mentor_profiles.custom_meeting_url`
        # sit on a venue that was not custom.
        CheckConstraint(
            "(application_stage = 'other') = (custom_stage_label IS NOT NULL)",
            name="custom_stage_label_matches_stage",
        ),
    )


class SessionTypeBookingConfig(TimestampMixin, Base):
    """How long a session type runs, and how much notice it needs.

    **``duration_minutes`` is the single source of truth for duration.** Legacy
    carried it in two other places — ``CalendarSettings.meetingDuration-TxT`` and
    a mentor-level field — and both are dropped.

    **Venue is not here any more.** It was a per-offering label for two releases;
    it is now ``session_types.conferencing_option_id``, a composite reference to
    something the mentor configured. Package D21 called it an inherit, and it has
    ended up as one — from the mentor's default rather than from another offering.

    **Surrogate ``id``, where the package makes ``session_type_id`` the primary
    key.** ADR 0015 admits no exception, and the invariant that key carried is
    re-declared as ``UNIQUE`` below. This rule has been overridden twice before
    with every gate green, which is why it is now asserted against the live
    schema rather than trusted to prose.
    """

    __tablename__ = "session_type_booking_configs"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    session_type_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("session_types.id", ondelete="CASCADE"), nullable=False
    )

    duration_minutes: Mapped[int] = mapped_column(nullable=False)
    #: How far ahead a mentee must book. **The platform rule is 24 hours — no
    #: same-day booking — and this default is where it lives**, because the ETL
    #: never sets the column and no endpoint could until offering writes shipped.
    #: It defaulted to 120 through M2—M4 and every migrated offering took it,
    #: which quietly permitted booking two hours out against a rule of
    #: twenty-four.
    #:
    #: **The `CHECK` below is sanity, not policy.** The product rule — 24h floor,
    #: 72h ceiling, the mentor's choice per session type — is enforced at the
    #: Pydantic boundary on the write schema, and moves to `booking_policies`
    #: when that table lands. A `CHECK` carrying it would need a migration every
    #: time the product changed its mind.
    min_notice_minutes: Mapped[int] = mapped_column(nullable=False, server_default=text("1440"))
    #: Whether a booking against *this offering* waits for the mentor —
    #: **or null, meaning follow the mentor's own setting.**
    #:
    #: ``mentor_profiles.requires_booking_confirmation`` is the authority. This
    #: column exists so one offering can auto-confirm while another waits: a free
    #: intro booked instantly, a paid review looked at first.
    #:
    #: **Null means inherit, and that is not the state D88 argued against.** D88
    #: moved this column here and rejected a nullable boolean, on the grounds
    #: that *a boolean has no room for a third state, so null would be
    #: indistinguishable from false to any reader who forgot the cascade*. That
    #: was right about inheriting from a **primary offering**, because a mentor
    #: may hold live offerings and no primary — the chain had a reachable, empty
    #: bottom, which is what also killed the venue cascade (#102). It is wrong
    #: about inheriting from the mentor: that row always exists.
    #:
    #: **No server default, deliberately.** A default of ``false`` under a
    #: nullable column would make every inserted row an override that happens to
    #: agree, and the inherit would be reachable only by writing null by hand.
    #:
    #: **Nothing reads it yet** (#21). The mentor column answers every question
    #: asked today; this one starts being consulted when an endpoint can set it.
    #: Ship it now anyway — a null costs nothing and the alternative is a second
    #: migration when the UI catches up.
    requires_booking_confirmation: Mapped[bool | None] = mapped_column(nullable=True)

    __table_args__ = (
        UniqueConstraint("session_type_id"),
        CheckConstraint("duration_minutes BETWEEN 5 AND 480", name="duration_minutes_valid"),
        # Thirty days. Wide on purpose: a sanity bound must never be the thing
        # blocking a product decision, and the 72-hour ceiling is already
        # expected to rise. `0` stays legal — it is what fixtures use to test the
        # slot maths without a notice window in the way.
        CheckConstraint("min_notice_minutes BETWEEN 0 AND 43200", name="min_notice_minutes_sane"),
    )


class Session(TimestampMixin, Base):
    """One booking, from the moment it is claimed through whatever it becomes.

    **``mentor_id`` and ``mentee_id`` stay on this row rather than moving into
    ``session_participants``, and that is not denormalisation** (package D4).
    ``EXCLUDE`` operates on columns of a single table and cannot reach a joined
    one, so moving ``mentor_id`` out would forfeit database-level double-booking
    prevention — the exact bug class this migration exists to escape. The
    alternative is a ``BEFORE INSERT`` trigger running an overlap query, which is
    race-prone without extra locking and is application logic wearing a
    constraint's clothes.

    The columns therefore *express a domain invariant*: a session is 1:1 between
    exactly one mentor and one mentee. If that changes — cohort sessions, group
    workshops — the migration is to keep ``mentor_id`` as the host, drop
    ``mentee_id``, and use participants for attendees.

    **``status`` is not derived from attendance** (package D5). It is a lifecycle
    state that exists before anyone attends: a session is
    ``PENDING_MENTOR_APPROVAL`` at creation and ``CANCELLED`` if called off, and
    neither has attendance to derive from.

    **``session_type_id`` is nullable here and becomes ``NOT NULL`` in a later
    migration**, once every legacy session carries the auto-created type. That is
    expand/contract across two releases, deliberately — the contract step is not
    in this pull request.

    **``meeting_url`` is a real column and legacy fills it.** The package's field
    mapping sends ``Meeting venue`` and ``meetinglink`` to ``meeting_provider``,
    which is wrong: both hold a URL on every dev row, and the two agree on all 103
    linked pairs. The provider is *derived* from the URL host at transform time.

    Dropped as derivable from ``starts_at``: ``datePicked``, ``datePickedText``,
    ``slotBookedTime``, ``session time``, ``Weekday(number)``. Dropped as
    duplicates: ``SessionID``, ``TrackID``, ``Mentor(this session)``,
    ``Mentee(userdatatype)``, ``Mentor(userdatatype)``, ``Canceled``.
    """

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )

    #: Straight to `users`, not to `mentor_profiles`. A session outlives the
    #: mentor's profile row and remains part of the mentee's history either way,
    #: which is the opposite of the guarantee `session_types` wants.
    mentor_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    mentee_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    #: Who clicked book. Diverges from `mentee_id` when a mentor or an admin
    #: books on someone's behalf. **Null on every migrated row**: legacy
    #: `Creator` reads `(App admin)` on all 105 dev bookings — a workflow, not a
    #: person — and settled decision #60 makes `Creator` a cross-check rather
    #: than an attribution.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    session_type_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("session_types.id", ondelete="RESTRICT")
    )

    status: Mapped[SessionStatus] = mapped_column(
        str_enum(SessionStatus), nullable=False, server_default=text("'pending_mentor_approval'")
    )

    starts_at: Mapped[datetime.datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    #: Copied onto the session at booking rather than read live from the config,
    #: for the reason settled decision #10 gives about a mentor's rate: a later
    #: edit to the session type must not silently rewrite what was agreed.
    duration_minutes: Mapped[int] = mapped_column(nullable=False)

    topic: Mapped[str | None] = mapped_column(Text)
    booking_message: Mapped[str | None] = mapped_column(Text)

    meeting_provider: Mapped[MeetingProvider | None] = mapped_column(str_enum(MeetingProvider))
    #: Generated per session at confirmation. A static personal room means
    #: back-to-back sessions share it and an early joiner walks into the previous
    #: one — a privacy incident rather than a UX annoyance (package D21). Every
    #: legacy row carries exactly that: 105 dev bookings share two Meet URLs.
    meeting_url: Mapped[str | None] = mapped_column(Text)
    external_room_id: Mapped[str | None] = mapped_column(Text)
    external_calendar_event_id: Mapped[str | None] = mapped_column(Text)

    rescheduled_from_session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("sessions.id", ondelete="RESTRICT")
    )

    legacy_bubble_id: Mapped[str | None] = mapped_column(Text, unique=True)

    __table_args__ = (
        CheckConstraint("mentor_id <> mentee_id", name="no_self_booking"),
        # Settled decision #100, and the last vocabulary to convert. `status` is
        # named by all three partial indexes below **and** by the exclusion
        # constraint, so this column is the one whose predicate transcriptions
        # have to be right — see the migration.
        CheckConstraint(
            check_is_known("status", SessionStatus),
            name="status_is_known",
        ),
        CheckConstraint("duration_minutes BETWEEN 5 AND 480", name="duration_minutes_valid"),
        # **The constraint this table exists for.**
        #
        # Legacy did prevent double-booking, and mostly from the frontend
        # (settled decision #84) — which cannot see two people clicking in the
        # same second and is skipped by any path avoiding that screen. This moves
        # the control to the database, where it can be neither raced nor
        # bypassed. Checking first and inserting after would reintroduce exactly
        # the race the guardrail forbids.
        #
        # **`session_window` is a function this project owns**, and it exists
        # because the package's expression cannot be built at all:
        # `timestamptz + interval` is STABLE — a day or month component depends
        # on the session's `TimeZone` — and an index expression must be
        # IMMUTABLE. A `GENERATED ALWAYS` column fails identically. Minutes-only
        # arithmetic *is* timezone-independent, which is what earns the label,
        # and two tests hold it: one on `provolatile`, one on the behaviour.
        #
        # Named for the table, following `availability_rules_no_overlap`. The
        # naming convention has no `ex` key, so an exclusion constraint carries a
        # literal name or none at all.
        ExcludeConstraint(
            ("mentor_id", "="),
            (text("session_window(starts_at, duration_minutes)"), "&&"),
            name="sessions_no_mentor_double_booking",
            using="gist",
            where=text(LIVE_STATUSES),
        ),
        # The three reads that matter, all partial. `alembic check` cannot
        # compare a predicate, so each is asserted against `pg_indexes`.
        Index(
            "ix_sessions_mentor_upcoming",
            "mentor_id",
            "starts_at",
            postgresql_where=text(LIVE_STATUSES),
        ),
        Index(
            "ix_sessions_mentee_upcoming",
            "mentee_id",
            "starts_at",
            postgresql_where=text(LIVE_STATUSES),
        ),
        Index(
            "ix_sessions_mentor_completed",
            "mentor_id",
            postgresql_where=text("status = 'completed'"),
        ),
        Index("ix_sessions_starts_at", "starts_at"),
        # **The fourth read, and the only one that is not partial.** The public
        # slots endpoint subtracts a mentor's sessions whatever their status, so
        # the three above serve it not at all: between them they cover the live
        # statuses and `completed`, leaving `cancelled`, `declined`, `expired`
        # and `no_show` in no per-party index. Measured at 20,000 rows, that read
        # was a sequential scan at 13.2ms and is 1.3ms with this.
        #
        # `gist` because the predicate is `&&` against a `tstzrange`, which btree
        # cannot answer, and the same `session_window` the exclusion constraint
        # above uses — one definition of a session's window, not two.
        Index(
            "ix_sessions_mentor_window",
            "mentor_id",
            text("session_window(starts_at, duration_minutes)"),
            postgresql_using="gist",
        ),
        # Settled decision #100. **Nullable, and the constraint needs no special
        # case for it**: `NULL IN (...)` is unknown, and a CHECK rejects only what
        # is false. A null here means the venue is not decided yet, which stays a
        # legal state.
        CheckConstraint(
            check_is_known("meeting_provider", MeetingProvider),
            name="meeting_provider_is_known",
        ),
    )


class SessionParticipant(TimestampMixin, Base):
    """Attendance. One row per person per session.

    Replaces four parallel legacy columns — ``Last Joined(mentee)``,
    ``Last Joined(Mentor)``, ``TrackStatus(mentee)``, ``TrackStatus(Mentor)``.
    The two representations agree on all 267 dev tracker rows, so the mapping
    needs no tie-break.

    Rows are written in the **same transaction** as the session insert, so they
    can never disagree with ``mentor_id`` and ``mentee_id``. The partial unique
    index below is what catches it if they ever do.

    **Surrogate ``id``, where the package uses ``PRIMARY KEY (session_id,
    user_id)``.** ADR 0015 admits no exception; the pair survives as ``UNIQUE``,
    which is the invariant the composite key actually carried.
    """

    __tablename__ = "session_participants"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    role: Mapped[SessionRole] = mapped_column(str_enum(SessionRole), nullable=False)
    joined_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    #: No legacy source. Bubble records an arrival and never a departure, so this
    #: is null on every migrated row and `AttendanceStatus.LEFT_EARLY` is first
    #: written by the product.
    left_at: Mapped[datetime.datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    attendance_status: Mapped[AttendanceStatus] = mapped_column(
        str_enum(AttendanceStatus), nullable=False, server_default=text("'pending'")
    )

    __table_args__ = (
        UniqueConstraint("session_id", "user_id"),
        # Catches drift between `sessions.mentor_id` and the participant rows.
        # Partial and unique: exactly one mentor per session, any number of
        # mentees once group sessions exist.
        Index(
            "ix_session_participants_one_mentor",
            "session_id",
            unique=True,
            postgresql_where=text("role = 'mentor'"),
        ),
        Index("ix_session_participants_user", "user_id"),
        # Settled decision #100. **`role` is the one that matters here**: the
        # unique index above is partial on `role = 'mentor'`, so a value outside
        # the vocabulary would sit outside the predicate and the "exactly one
        # mentor per session" invariant would simply not apply to that row.
        CheckConstraint(
            check_is_known("role", SessionRole),
            name="role_is_known",
        ),
        # `LEFT_EARLY` has no legacy source and is first written by the product,
        # which is precisely the kind of value #100 exists to keep removable.
        CheckConstraint(
            check_is_known("attendance_status", AttendanceStatus),
            name="attendance_status_is_known",
        ),
    )


class SessionEvent(Base):
    """Every state a session passed through, append-only.

    Replaces the scattered legacy flags: ``bookingRequestAccepted``,
    ``SessionCancel (Y/N)``, ``Canceled By``, ``Session Cancel/Decline Message``,
    ``Expiration``, ``statusApproved-DeclinedDate``.

    **``reason_code`` and ``reason_text`` are two different fields** (package
    D6). The text is what a human wrote; the code is what policy runs on —
    ``mentor_unavailable`` refunds, ``mentee_no_longer_needed`` within 24 hours
    does not. Legacy supplies only the text, so every migrated event carries a
    null code, which is why the index over it is partial.

    **No ``TimestampMixin``, and no ``updated_at``.** This table is append-only:
    a row states what happened at a moment, and a fact that can be edited is not
    a log. The same reasoning already shaped ``mentor_status_events``, and
    ``APPEND_ONLY`` in ``tests/unit/test_models.py`` is what holds it.

    ``actor_id`` is nullable — null means the system acted, which is honest for
    an expiry job and better than inventing a system user.

    **This does not project onto ``sessions.status``.** ``mentor_status_events``
    has ``trg_apply_mentor_status`` because two columns there hold a state a
    constraint must see. Here the lifecycle column is written by the same
    transaction that writes the event, and a trigger would be a second mechanism
    for one fact. If that changes, it changes with a trigger and a test that
    inserts an event directly — not with a helper everybody remembers to call.
    """

    __tablename__ = "session_events"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("sessions.id", ondelete="RESTRICT"), nullable=False
    )

    #: Null on the creation event, where there is no prior state.
    from_status: Mapped[SessionStatus | None] = mapped_column(str_enum(SessionStatus))
    to_status: Mapped[SessionStatus] = mapped_column(str_enum(SessionStatus), nullable=False)

    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    actor_type: Mapped[ActorType] = mapped_column(
        str_enum(ActorType), nullable=False, server_default=text("'user'")
    )

    reason_code: Mapped[SessionReasonCode | None] = mapped_column(str_enum(SessionReasonCode))
    reason_text: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict[str, object]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'")
    )

    created_at: Mapped[datetime.datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        # One session's history, oldest first — the order a timeline renders in.
        Index("ix_session_events_session", "session_id", "created_at"),
        # "What share of mentor-side cancellations were scheduling conflicts."
        # Partial, because a null code is the common case on migrated rows.
        Index(
            "ix_session_events_reason",
            "reason_code",
            "created_at",
            postgresql_where=text("reason_code IS NOT NULL"),
        ),
        # Settled decision #100. The index above needs no rewrite: its predicate
        # is `reason_code IS NOT NULL` and names no enum literal, so it rebuilds
        # with the table — `pg_depend` lists neither type against an index.
        CheckConstraint(
            check_is_known("actor_type", ActorType),
            name="actor_type_is_known",
        ),
        # Nullable, and the `IN` form permits that without a special case:
        # migrated cancellation events carry no code, because legacy had no coded
        # field behind its free-text cancel message.
        CheckConstraint(
            check_is_known("reason_code", SessionReasonCode),
            name="reason_code_is_known",
        ),
        # Settled decision #100. `from_status` is null on the creation event,
        # where there is no prior state; the `IN` form permits that.
        CheckConstraint(
            check_is_known("from_status", SessionStatus),
            name="from_status_is_known",
        ),
        CheckConstraint(
            check_is_known("to_status", SessionStatus),
            name="to_status_is_known",
        ),
    )
