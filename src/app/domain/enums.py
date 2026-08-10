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

    **Only ``CUSTOM`` ever stores a URL.** ``GOOGLE_MEET`` and ``DAILY`` both
    create a per-session link at confirmation — Meet through the calendar
    integration, Daily through its API — so a stored link would be redundant for
    them and actively harmful: a static personal room means back-to-back sessions
    share a room and an early joiner walks into the previous one. That is why
    ``custom_meeting_url`` is gated by a CHECK rather than by convention.

    ``ZOOM`` ships with no legacy source. Legacy offered only "Edufurther Video"
    (Daily) and "External Video Tool" (custom), and every stored link was a
    Google Meet URL left behind as residue — so the migration writes no
    ``custom_meeting_url`` at all.
    """

    GOOGLE_MEET = "google_meet"
    DAILY = "daily"
    ZOOM = "zoom"
    CUSTOM = "custom"


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
