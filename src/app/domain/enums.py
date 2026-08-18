"""Closed vocabularies shared by the whole application.

**These live in ``domain/`` rather than beside the models, and that placement is
the point.** Each one is a fact about the product — what roles exist, which
providers can be linked — not a fact about PostgreSQL. Domain services will need
``PrimaryRole`` and cannot import ``infra/``, so a definition that starts in
``infra/db/models/`` would have to move the first time a business rule mentions
it. One definition, in the layer that owns the meaning, and ``infra`` imports it
to build the database type.

``StrEnum`` so a member compares equal to its wire value: ``PrimaryRole.MENTEE ==
"mentee"`` is true, which keeps JSON serialisation and string comparison honest
without a converter at every boundary.

**Values, not names, reach the database.** SQLAlchemy's ``Enum`` defaults to the
*member name* — ``MENTEE`` — which would create a PostgreSQL type whose labels are
uppercase and disagree with `docs/edufurther-migration/`. Every ``Enum`` column in
``infra`` therefore passes ``values_callable``; the helper for that lives beside
the models, since it is a SQLAlchemy concern.

The value lists are transcribed from `schema/00_foundation.sql` and must stay
identical to it. Adding a member here is a migration, not an edit — PostgreSQL
enum labels are schema.
"""

from enum import StrEnum


class PrimaryRole(StrEnum):
    """Which dashboard a user lands on. **Never an authorization check.**

    Authorization is profile existence: someone can be booked when an approved
    ``mentor_profiles`` row exists, and can book when a ``mentee_goals`` row does
    (package D2). A role column has to be kept consistent with those tables and
    can silently disagree with them; existence cannot, which is what makes dual
    roles free rather than a feature.

    The legacy data already disagrees with itself — the dev extract has a user
    whose ``Role`` is Mentee and who has a linked Mentor record — so this is not
    a hypothetical hazard being designed around.

    ``WHERE primary_role = 'mentor'`` in a permission check is a bug.
    """

    MENTEE = "mentee"
    MENTOR = "mentor"


class AdminRole(StrEnum):
    """Elevated access, held in ``admin_users`` rather than on the user row.

    The legacy design put an admin option set on ``User``, which made the grant
    un-revocable and left no record of who granted it.
    """

    SUPER_ADMIN = "super_admin"
    MENTOR_APPROVAL = "mentor_approval"
    LIMITED_ACCESS = "limited_access"


class AuthProvider(StrEnum):
    """External identity providers that can be linked to an account.

    Deliberately does not include email. An email login produces no
    ``auth_identities`` row — Supabase owns that path (ADR 0009), and the legacy
    ``Registration format`` value ``Email`` maps to the absence of a row rather
    than to a member here.
    """

    GOOGLE = "google"
    LINKEDIN = "linkedin"


class LanguageProficiency(StrEnum):
    """How well a user speaks a language.

    Ordered strongest to weakest as written, but **nothing depends on that
    order** — it is a set, and PostgreSQL enum ordering is label-declaration
    order, which is a trap worth not relying on.
    """

    NATIVE = "native"
    FLUENT = "fluent"
    CONVERSATIONAL = "conversational"
    BASIC = "basic"


class LegalDocumentType(StrEnum):
    """Which document a version of the terms represents.

    All four ship now even though only ``TERMS_OF_SERVICE`` has a consent to
    record at cutover. An enum member costs nothing until a row uses it, and
    ``ALTER TYPE ... ADD VALUE`` cannot run inside a transaction on older
    PostgreSQL — the asymmetry that makes enum members cheaper to declare than to
    add later.
    """

    TERMS_OF_SERVICE = "terms_of_service"
    PRIVACY_POLICY = "privacy_policy"
    MENTOR_AGREEMENT = "mentor_agreement"
    COMMUNITY_GUIDELINES = "community_guidelines"


class LookupStatus(StrEnum):
    """Curation state of a catalogue row that users can create.

    Types ``institutions.status`` and ``scholarship_programs.status`` — the two
    M2 lookups whose rows come from users. ``degree_levels`` and
    ``service_offerings`` have no status column because they are closed
    vocabularies the product defines; nobody can add a row, so there is nothing
    to curate.

    ``MERGED`` is not a soft delete, and the difference is the point. The losing
    row survives and ``merged_into_id`` points at the winner, so a client holding
    a cached reference to "chevening scholarship" still resolves instead of
    404ing. Without it, deduplicating "Chevening", "Chevening Award" and
    "chevening scholarship" into one row breaks every reference taken before the
    merge (ADR 0008; package D15, which makes the merge path mandatory rather
    than optional).

    Shipped one phase ahead of the five below, per settled decision #21: it was
    the only vocabulary M2's *lookup* tables constrained. The rest arrived with
    the profile tables that use them.

    ``scholarship_relationship`` never ships. Its only consumer was
    ``user_scholarship_experience``, and the legacy field behind that table has
    no option set, no values on any row, and therefore nothing to migrate.
    """

    APPROVED = "approved"
    PENDING_REVIEW = "pending_review"
    MERGED = "merged"
    REJECTED = "rejected"


class ApprovalStatus(StrEnum):
    """Whether a mentor's application has been accepted.

    **Distinct from being listed, and the pair is not redundant.** Approval is a
    judgement the platform made once; listing is whether the mentor currently
    wants to be found. An approved mentor who paused is `approved` + `unlisted`,
    which no single flag can express.

    Legacy carried this as ``approvedText``, a text "Yes" — so the migration sees
    only ``APPROVED`` and ``DECLINED`` has no legacy instance.
    """

    PENDING = "pending"
    APPROVED = "approved"
    DECLINED = "declined"


class ListingStatus(StrEnum):
    """Whether a mentor appears in search.

    Collapsed from two booleans the package's first draft carried. ``is_available``
    was **derivable** from availability rules, exceptions and existing sessions,
    and storing it recreated the drift that made the legacy front-search table
    untrustworthy.

    **This is not profile-page access.** A mentee with a completed session must
    still see that mentor's page or their history breaks and past reviews 404, so
    access is a rule about the *viewer* — listed, or has a session with them, or
    is an admin — rather than a column here.
    """

    LISTED = "listed"
    UNLISTED = "unlisted"


class UnlistedReason(StrEnum):
    """Why a mentor is not in search. **Internal — never shown to a mentee.**

    Drives admin dashboards and re-engagement. The column defaults to
    ``NEVER_APPROVED``, which is right for a new signup and **wrong for every
    migrated mentor**: the two unlisted mentors in the dev extract are
    ``approvedText = Yes``, so they were approved and then turned themselves off.
    The transform sets this explicitly rather than inheriting the default.
    """

    MENTOR_PAUSED = "mentor_paused"
    ADMIN_REVIEW = "admin_review"
    DORMANT = "dormant"
    NEVER_APPROVED = "never_approved"


class MentorStatusType(StrEnum):
    """What a `mentor_status_events` row records.

    Two dimensions in one value, and unambiguously so: `approved`/`declined`
    concern approval, `listed`/`unlisted` concern listing. **A separate
    `dimension` column would be a second representation of something the value
    already says**, and two representations of one fact eventually disagree.

    Each row states only what changed and copies nothing forward, so two
    transitions on different dimensions cannot combine into a state that never
    existed. Current state lives on `mentor_profiles`, kept in step by
    `trg_apply_mentor_status`.
    """

    APPROVED = "approved"
    DECLINED = "declined"
    LISTED = "listed"
    UNLISTED = "unlisted"


class VerificationStatus(StrEnum):
    """Whether a self-reported award has been checked. **Nothing checks one yet.**

    The package's decision for this phase is deliberate: do not verify, label
    clearly. Every award defaults to ``UNVERIFIED`` and **nothing renders a
    checkmark**, because every verified claim is manual admin work and the queue
    never empties.

    The other three members ship unused so that switching verification on later
    is a feature flag rather than a migration. The label belongs at the field
    ("Awards — self-reported"), not in a footer: a checkmark beside "Chevening
    Scholar" reads as endorsement whatever a tooltip says, which becomes a
    liability question once money is involved.
    """

    UNVERIFIED = "unverified"
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"


class MeetingProvider(StrEnum):
    """Where a session happens.

    ``GOOGLE_MEET`` and ``DAILY`` both create a per-session link at confirmation
    — Meet through the calendar integration, Daily through its API — so a stored
    link would be redundant for them and actively harmful: a static personal room
    means back-to-back sessions share a room and an early joiner walks into the
    previous one.

    **``CUSTOM`` now has somewhere to keep a URL**, and that is what
    ``mentor_conferencing_options.custom_url`` is for. It had none for two
    releases: ``mentor_profiles.custom_meeting_url`` was the only such column and
    D88's contract step removed it — as a removal rather than a move, because
    nothing had ever written it — which left an offering on ``CUSTOM`` resolving
    to a venue with no way to reach it. A **symmetric** ``CHECK`` on the new table
    makes that state unrepresentable in both directions.

    **This enum is history; :class:`ConferencingProvider` is choice.** A mentor
    selects from the latter, which omits ``ZOOM``; ``sessions.meeting_provider``
    keeps this one, because a session that happened on a venue keeps naming it
    even after the venue stops being selectable.

    ``ZOOM`` ships with no legacy source. Legacy offered only "Edufurther Video"
    (Daily) and "External Video Tool" (custom), and every stored link was a
    Google Meet URL left behind as residue — so the migration wrote no custom
    URL at all, which is why removing the column lost nothing.
    """

    GOOGLE_MEET = "google_meet"
    DAILY = "daily"
    ZOOM = "zoom"
    CUSTOM = "custom"


class ConferencingProvider(StrEnum):
    """What a mentor can **host on** — not where a session happened.

    **Deliberately not :class:`MeetingProvider`, and the difference is tense.**
    That enum answers *where did this session take place*, so it keeps every value
    the platform has ever written, including ones nothing can create today.
    This one answers *what may a mentor select right now*, and a value belongs
    here only once something can produce a joinable session from it.

    ``ZOOM`` is therefore absent. It has no integration and nothing can mint a
    link for it, so offering it would let a mentor choose a venue that cannot
    host — the exact failure the symmetric ``custom_url`` constraint closes from
    the other direction. ``ZOOM`` and ``TEAMS`` join **at the point they have a
    connection behind them**, which is what the three connection columns on
    ``mentor_conferencing_options`` exist to hold.

    The two vocabularies overlap and must not drift apart by accident, so
    ``test_conferencing_providers_are_meeting_providers`` asserts every member
    here is a member there. That is non-negotiable #8's *pin the copies* form:
    the values genuinely are duplicated, and the test is what makes the
    duplication safe.
    """

    GOOGLE_MEET = "google_meet"
    DAILY = "daily"
    CUSTOM = "custom"


class ApplicationStage(StrEnum):
    """Where in their application a mentee is, for an offering aimed at a stage.

    **The first vocabulary this project designed rather than received.** The
    column shipped as free `text` with no constraint and no value in any row,
    withheld from the public contract precisely because publishing it *"would
    commit this contract to a shape nobody has designed"*. The UI designed it, so
    that reason has lapsed and the column stops being free text.

    **`OTHER` is the one that costs something**, and it is kept deliberately. A
    closed set with no escape hatch pushes anything unanticipated into whichever
    member is least wrong, which is worse than an honest "something else" — and
    this vocabulary was drawn from fourteen mock screens rather than from data,
    so being incomplete is the expected case rather than the surprising one.

    Its label lives in ``session_types.custom_stage_label``, tied to it by a
    **symmetric** ``CHECK``: ``OTHER`` with no label renders a blank chip, and a
    named stage carrying a stale label is dead data that survives an edit. Both
    directions, for the same reason the conferencing constraint is symmetric —
    the one-directional form is what let a mentor be bookable with nowhere to
    meet.

    **Not a lookup table**, unlike ``service_offerings`` next door. The handoff's
    test applies: adding a value here requires a code change anyway, because
    nothing consumes a stage it does not know how to render — so it is a closed
    set, and #100 makes a closed set ``text`` + ``CHECK``.
    """

    EARLY_EXPLORATION = "early_exploration"
    DRAFTING_STAGE = "drafting_stage"
    POST_SUBMISSION = "post_submission"
    REVISIONS = "revisions"
    OTHER = "other"


class QuestionType(StrEnum):
    """What shape of answer an intake question expects.

    **`MULTI_CHOICE` ships with nothing using it**, and that is a departure from
    how this project usually decides. Settled decision #21 says nothing ships
    before the phase that needs it, and the UI's intake screens use two of these
    three.

    Two things make it right here. The package is canonical (ADR 0007) and
    declares all three plus `session_type_question_options`, so dropping one
    would be an undeclared divergence rather than a deferral. And **#100's
    sub-rule against adding an unused value has been retired** — it existed
    because `ALTER TYPE ... ADD VALUE` is permanent and PostgreSQL has no
    `DROP VALUE`, and #107 recorded that the argument "does not survive its
    removal" once the conversion made a vocabulary a freely-altered `CHECK`. The
    cost of being early is now one migration rather than forever.

    So the option rows have a table to live in the day a mentor needs them, and
    `exactly_one_answer_form` already knows how such an answer is shaped.
    """

    FREE_TEXT = "free_text"
    FILE_UPLOAD = "file_upload"
    MULTI_CHOICE = "multi_choice"


class IntakeStatus(StrEnum):
    """How far a mentee has got with the form attached to their booking.

    `REVIEWED` is the mentor's acknowledgement, not a second submission — the
    mentee cannot move a form out of it, and nothing here enforces transitions.
    That belongs with the endpoints that write the column.

    **`DRAFT` is the default and is why the row exists early.** A submission is
    created with the booking so answers have somewhere to go while the mentee is
    still typing; the alternative is holding partial answers client-side and
    losing them, which is what an intake form is least able to afford.
    """

    DRAFT = "draft"
    SUBMITTED = "submitted"
    REVIEWED = "reviewed"


class AvailabilityExceptionType(StrEnum):
    """What an exception does to a mentor's recurring availability.

    The two are not opposites of one flag. ``BLOCK`` subtracts from the weekly
    rules — a holiday, an exam period — and ``OVERRIDE`` adds a window that no
    rule describes, which is how a mentor offers a one-off slot without editing
    the schedule they keep every other week. A single boolean could express
    either one but not both, because they compose in one direction only: a block
    removes time a rule granted, and an override grants time no rule mentions.

    **Every migrated row is a ``BLOCK``.** Legacy ``CalendarExtra`` carries only
    ``block-Date(s)`` and has no field that could mean anything else, so
    ``OVERRIDE`` ships with no source data and is first written by the product.
    That is the same position ``ZOOM`` holds in :class:`MeetingProvider`, and it
    is recorded for the same reason: a member with no legacy rows is a member the
    ETL must never invent, and a reconciliation that finds one has found a bug.
    """

    BLOCK = "block"
    OVERRIDE = "override"


class SessionStatus(StrEnum):
    """Where a session sits in its lifecycle. **Never derived from attendance.**

    Status exists *before* anyone attends: a session is ``PENDING_MENTOR_APPROVAL``
    at creation and ``CANCELLED`` if called off, and neither has attendance data to
    derive from (package D5). The relationship runs one way only — attendance
    *informs* ``CONFIRMED -> COMPLETED | NO_SHOW`` and defines none of the rest.

    **``NO_SHOW`` here is not the same fact as
    :class:`AttendanceStatus.NO_SHOW`.** This one says the session did not happen;
    that one says one named person did not arrive. A session where the mentee
    attends and the mentor does not has one participant ``ATTENDED`` and one
    ``NO_SHOW``, and exactly one session-level outcome.

    **The legacy vocabulary is measured on the dev extract only**, which holds five
    values — ``Canceled``, ``Missed``, ``Declined``, ``Completed`` and ``Pending``.
    Dev is test data and 105 rows against production's 1,073, so it is evidence of
    which values *occur*, not proof of which *exist*. The transform re-derives the
    vocabulary from the production extract and raises on a value it has not seen,
    rather than defaulting.

    **``WITHDRAWN`` is not ``CANCELLED``, and the transition rule is narrow.**
    Only ``PENDING_MENTOR_APPROVAL -> WITHDRAWN``: a mentee taking back a request
    the mentor has not answered yet. A *confirmed* session called off is
    ``CANCELLED``, whoever calls it off — the mentor has already committed time,
    and that is the fact the two statuses separate. Collapsing them would put a
    request nobody had accepted into the same bucket as a booking broken after
    agreement, which carries different policy for refunds and for
    mentor-reliability statistics.

    Nothing enforces transitions in the schema, here or anywhere else; the rule
    lives at the endpoint that writes the status. It is recorded here because the
    value is meaningless without it.
    """

    PENDING_MENTOR_APPROVAL = "pending_mentor_approval"
    CONFIRMED = "confirmed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    DECLINED = "declined"
    EXPIRED = "expired"
    NO_SHOW = "no_show"
    #: A mentee withdrawing a request before the mentor answers. **Outside
    #: ``LIVE_STATUSES``**, with ``EXPIRED``, ``DECLINED`` and ``CANCELLED``: a
    #: withdrawn request holds no booking slot, so the mentor's time frees
    #: immediately and the double-booking constraint ignores it.
    #:
    #: Added after settled decision #100 finished, which is what made it cheap —
    #: three ``CHECK`` swaps in one reversible migration rather than a permanent
    #: ``ALTER TYPE ... ADD VALUE``. #100's rule *"do not add a value until
    #: something writes it"* was justified by that permanence and does not
    #: survive it; the value lands ahead of the withdraw flow deliberately.
    WITHDRAWN = "withdrawn"


class SessionRole(StrEnum):
    """What a person is to a session, on their ``session_participants`` row.

    ``MENTOR`` and ``MENTEE`` duplicate ``sessions.mentor_id`` and
    ``sessions.mentee_id`` deliberately: the columns carry the 1:1 domain
    invariant and the exclusion constraint that depends on it (package D4), while
    the participant rows carry attendance. A partial unique index on
    ``(session_id) WHERE role = 'mentor'`` is what stops the two representations
    drifting.

    **``OBSERVER`` ships with no legacy source and no product feature yet**, for
    the reason :class:`MeetingProvider.ZOOM` and
    :class:`AvailabilityExceptionType.OVERRIDE` already carry: a label costs
    nothing to add and cannot be removed — ``ALTER TYPE ... DROP VALUE`` is
    unimplemented in every PostgreSQL version (settled decision #31). It is here
    because group sessions are a named future capability, and a member with no
    legacy rows is one the ETL must never invent.
    """

    MENTOR = "mentor"
    MENTEE = "mentee"
    OBSERVER = "observer"


class AttendanceStatus(StrEnum):
    """Whether one participant turned up. Per person, never per session.

    ``PENDING`` is the state before the session runs, which is why it is the
    default rather than a nullable column: "we do not know yet" is a real answer
    and distinguishable from ``NO_SHOW``.

    **Legacy carries exactly two of these four.** ``TrackStatus(mentee)`` and
    ``TrackStatus(Mentor)`` are ``yes``/``no``, and they agree with the presence
    of ``Last Joined`` on all 267 dev tracker rows — so the mapping is
    unambiguous and needs no tie-break. ``LEFT_EARLY`` has no legacy field at all:
    nothing in Bubble records a departure, so ``session_participants.left_at`` is
    null on every migrated row and this member is first written by the product.
    """

    PENDING = "pending"
    ATTENDED = "attended"
    NO_SHOW = "no_show"
    LEFT_EARLY = "left_early"


class SessionReasonCode(StrEnum):
    """Why a session changed state, as a value you can ``GROUP BY``.

    **This is not ``reason_text`` and does not replace it** (package D6). The text
    is what a human wrote — *"Sorry, conference clash"* — and the code is what
    policy runs on: ``MENTOR_UNAVAILABLE`` refunds, ``MENTEE_NO_LONGER_NEEDED``
    within 24 hours of the start does not. "What share of mentor-side
    cancellations are scheduling conflicts" decides whether reschedule flows get
    built, and free text cannot answer it without somebody reading 200 rows.

    **Legacy supplies none of these.** ``Session Cancel/Decline Message`` is free
    text and becomes ``reason_text``; there is no coded field behind it anywhere
    in ``SessionBooking`` or ``SessionTracker``. Migrated cancellation events
    therefore carry a null ``reason_code``, which is why the column is nullable
    and why the index over it is partial.
    """

    MENTOR_UNAVAILABLE = "mentor_unavailable"
    MENTEE_NO_LONGER_NEEDED = "mentee_no_longer_needed"
    SCHEDULING_CONFLICT = "scheduling_conflict"
    TECHNICAL_ISSUE = "technical_issue"
    MENTOR_NO_SHOW = "mentor_no_show"
    MENTEE_NO_SHOW = "mentee_no_show"
    EXPIRED_NO_RESPONSE = "expired_no_response"
    RESCHEDULED = "rescheduled"
    ADMIN_ACTION = "admin_action"


class ActorType(StrEnum):
    """What kind of thing caused a session event.

    It pairs with a **nullable** ``actor_id``: null means no person acted, which
    is the honest record for an expiry job and is better than inventing a system
    user (package D6). The same reasoning already put a null ``granted_by`` on
    every migrated ``admin_users`` row.

    The type is not redundant against ``actor_id IS NULL``. ``SYSTEM`` and ``API``
    are both actor-less, and an admin acting through the console is a different
    fact from the same human acting as themselves — which is exactly the
    distinction an audit trail exists to keep.
    """

    USER = "user"
    ADMIN = "admin"
    SYSTEM = "system"
    API = "api"
