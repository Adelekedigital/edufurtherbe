"""Identity: the ``users`` row and the five tables that hang directly off it.

The legacy ``User`` table did five jobs in ~30 columns — identity, profile, OAuth
credentials, billing and onboarding all on one row, so every profile edit touched
the same record as every login. These five tables plus ``admin_users`` and the
two legal tables are what it becomes.

**What is deliberately absent**, each for a recorded reason:

- ``password_hash``, ``auth_codes`` — Supabase owns code issuance, hashing and
  verification (ADR 0009 §7). A column we never write to is schema asserting an
  implementation we do not have.
- ``phone_e164``, ``phone_verified_at``, ``phone_country_code`` — no data, no
  consumer, and ADR 0009 explicitly defers phone verification. Same argument that
  removed ``password_hash``; applying it to one and not the other would be
  inconsistent.
- ``calendar_connections`` — its DDL lives in the package's availability file, its
  legacy ``composioAuthId`` values are known-dead (the Composio managed-auth
  outage in the failure log), and ADR 0004 has every mentor reconnect at first
  login regardless. It ships with M3.
- ``account_deletion_requests`` — no legacy data and no feature depends on it.
- The GIN full-text index on ``about_me`` — nothing searches until M2.
- ``legacy_created_at`` / ``legacy_modified_at`` — withdrawn. Bubble's Creation
  Date lands in ``created_at`` and its Modified Date in ``updated_at``, loaded
  with ``trg_set_updated_at`` disabled. See the M1 migration.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    CheckConstraint,
    ForeignKey,
    Index,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import AuthProvider, LanguageProficiency, PrimaryRole
from app.infra.db.base import Base, TimestampMixin
from app.infra.db.types import pg_enum


class User(TimestampMixin, Base):
    """Identity and authentication only. The hot path; kept lean."""

    __tablename__ = "users"

    # Ours, generated here, like every other table (ADR 0015).
    #
    # ADR 0009 §9 originally made this column the Supabase auth user id, with no
    # default. ADR 0014 reverses that: a provider's identifier as our primary key
    # would put a vendor-issued value in every foreign key of all sixty-six
    # tables, which is what tier-2's identifier rule — "translate at the
    # boundary; do not compare across spaces" — exists to prevent.
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )

    # The Supabase auth user id, and the only place it appears in this schema.
    #
    # Nullable, and that is load-bearing rather than lax: a migrated user exists
    # before they have ever logged in, so M1c inserts 1,200 rows without calling
    # Supabase at all. `auth_id IS NULL` means "has never authenticated", which
    # also makes first-login progress a query rather than a guess.
    #
    # Plain UNIQUE, not partial: PostgreSQL treats nulls as distinct, so any
    # number of un-provisioned users coexist, and Supabase never reuses an id, so
    # a soft-deleted user cannot block one.
    auth_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, unique=True)

    # citext, so `WHERE email = :x` is case-insensitive at every call site rather
    # than at the ones somebody remembered to lower(). Verified against
    # `compare_metadata` before adoption: reflection returns CITEXT, not TEXT, so
    # `alembic check` sees no spurious diff.
    email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    first_name: Mapped[str | None] = mapped_column(Text)
    last_name: Mapped[str | None] = mapped_column(Text)
    # Legacy `First and Last Name` is dropped: derived, and formatted in the API
    # layer where the caller's locale is known.

    # The legacy public profile handle (`sakiratu-adeleke`), absent from
    # `docs/bubble-data-model.md` and found only by reading the extract.
    #
    # This is precisely the globally-unique human-readable key that D32 warns
    # against while multi-tenancy is deferred: it cannot be namespaced later
    # without breaking every URL that used it. Carried anyway, deliberately —
    # the alternative is discarding live profile links.
    slug: Mapped[str | None] = mapped_column(Text)

    # UX hint. NEVER an authorization check — see PrimaryRole.
    primary_role: Mapped[PrimaryRole] = mapped_column(
        pg_enum(PrimaryRole),
        nullable=False,
        server_default=text("'mentee'"),
    )

    # IANA, e.g. Africa/Lagos. Plain text with a default rather than a lookup:
    # the tz database changes several times a year and a foreign key to it would
    # turn a zone rename into a failed insert. The dev extract holds clean IANA
    # values already, so the runbook's warning about non-IANA data did not fire
    # — but 43 rows is not 1,200, and the transform validates rather than trusts.
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'UTC'"))

    last_active_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    legacy_bubble_id: Mapped[str | None] = mapped_column(Text, unique=True)

    __table_args__ = (
        # THE PARTIAL UNIQUE INDEX PEOPLE FORGET. Without `WHERE deleted_at IS
        # NULL` a soft-deleted user permanently blocks their own email from
        # re-registering, and the failure is indistinguishable from "that address
        # is taken".
        Index(
            "ix_users_email_live",
            "email",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_users_slug_live",
            "slug",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND slug IS NOT NULL"),
        ),
        # No DESC. A single-column btree is scanned in either direction, so
        # `ORDER BY last_active_at DESC` uses this index exactly as well as a
        # descending one would — and an expression index is one more thing for
        # autogenerate to compare imprecisely, for no gain.
        Index(
            "ix_users_last_active_live",
            "last_active_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # Lowercase, digits and hyphens. Deliberately looser than the shape the
        # dev slugs actually take (`^[a-z0-9]+(-[a-z0-9]+)*$`, which all 39
        # satisfy): 43 dev rows are not 1,200 production rows, and a constraint
        # tightened to a 3% sample is a load failure waiting for cutover.
        #
        # What it does guarantee is the part that matters — URL-safety, and a
        # case-stable value, so the unique index above cannot be defeated by
        # `Sakiratu-Adeleke` alongside `sakiratu-adeleke`.
        # The length bound is not decoration. `slug` is unbounded `text` under a
        # unique btree, so without it a ~2,700-byte value fails as a raw storage
        # error (`index row size exceeds btree maximum`) rather than as
        # validation, and anything under that but over a few hundred characters
        # is accepted while being unusable in a URL.
        #
        # The name is bare. The `ck` convention renders it to
        # `ck_users_slug_is_url_safe`; passing that rendered form would produce
        # `ck_users_ck_users_slug_is_url_safe`.
        CheckConstraint(
            "slug IS NULL OR (slug ~ '^[a-z0-9-]+$' AND char_length(slug) BETWEEN 1 AND 60)",
            name="slug_is_url_safe",
        ),
    )


class UserProfile(TimestampMixin, Base):
    """Legacy ``PersonalInfo``, merged. Strictly 1:1, keyed on ``user_id``.

    **``user_id`` is the primary key, not a surrogate.** There is no second
    identifier to confuse with the user's, which removes the
    ``mentor_profile_id``-vs-``user_id`` ambiguity the legacy schema had: they
    are the same value everywhere.

    **Three countries, three meanings.** ``origin_country_code`` is nationality;
    ``current_country_code`` is where they live now and is new; study country
    lives on ``education_entries``, per degree (M2). A Nigerian mentee living in
    the UK has different visa questions, timezone and scholarship eligibility
    from one in Lagos.

    Note for the M1c transform: the package's field mapping sends legacy
    ``Country of Origin`` here and drops ``OriginCountry(text)`` as a duplicate.
    **The dev extract shows the reverse** — the coded field is empty on all 19
    rows and the text field carries the data. Following the mapping literally
    would migrate zero countries. The columns are unaffected; the source field is
    not.
    """

    __tablename__ = "user_profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    # UNIQUE carries the 1:1 invariant that the primary key used to carry. Drop
    # it and two profiles per user become legal, silently, with nothing surfacing
    # it until somebody reads the wrong one.
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    # Both MUST be re-hosted off Bubble before cutover. Note the check for that
    # cannot be `LIKE '%bubble%'`: the extract shows avatars on three hosts, and
    # eleven of them sit behind the custom domain app.edufurther.org, which
    # resolves to Bubble and would pass such a check.
    avatar_url: Mapped[str | None] = mapped_column(Text)
    banner_url: Mapped[str | None] = mapped_column(Text)

    about_me: Mapped[str | None] = mapped_column(Text)

    # Free text, not an enum. The dev extract holds "Male", "Female" and "I'd
    # rather not say"; a closed type here would make every future addition to how
    # someone describes themselves a database migration.
    gender: Mapped[str | None] = mapped_column(Text)

    # RESTRICT is spelled out rather than left to PostgreSQL's NO ACTION default
    # (ADR 0013). The two behave identically here — nothing defers a constraint —
    # so the clause buys no behaviour. What it buys is that a reader can tell a
    # decision from an omission, which on a reference table nobody expects to
    # delete from is otherwise impossible.
    origin_country_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("countries.id", ondelete="RESTRICT")
    )
    current_country_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("countries.id", ondelete="RESTRICT")
    )

    social_linkedin: Mapped[str | None] = mapped_column(Text)
    social_twitter: Mapped[str | None] = mapped_column(Text)
    social_youtube: Mapped[str | None] = mapped_column(Text)

    # Legacy emailitContact_id — an 18-digit numeric string, kept as text because
    # it is an opaque provider identifier and arithmetic on it is never correct.
    email_provider_contact_id: Mapped[str | None] = mapped_column(Text)

    legacy_bubble_id: Mapped[str | None] = mapped_column(Text, unique=True)

    __table_args__ = (
        Index("ix_user_profiles_origin_country", "origin_country_id"),
        Index("ix_user_profiles_current_country", "current_country_id"),
    )


class AuthIdentity(TimestampMixin, Base):
    """One row per linked external provider.

    Legacy ``Registration format`` was a single option set, so a user who signed
    up with Google could never also link LinkedIn. Kept as our own table despite
    Supabase having ``auth.identities``, because the reasons are ours rather than
    the vendor's: multiple providers per user, account linking on email
    collision, and — the one that decides it — ``Registration format`` is a
    legacy column that must land somewhere on import (ADR 0009 §8).

    **An email registration produces no row here.** The dev extract splits
    Email 37 / Google 6 / LinkedIn 0, which inverts ADR 0009's premise that
    Google was the dominant path; the production split is still uncounted and is
    an open question that record asks to be answered when M1 starts.

    **The uniqueness on ``(provider, provider_user_id)`` is not partial, and it
    interacts with soft delete in a way the deletion path must handle.** A
    soft-deleted user frees their email — that is what ``ix_users_email_live``
    is for — but their ``auth_identities`` row survives, because ``CASCADE``
    fires only on a hard delete that nothing performs. So the same Google
    subject cannot be linked again afterwards.

    This cannot be fixed with a partial index: the predicate would have to
    reference ``users.deleted_at``, which lives on another table. **It is a
    requirement on the deletion path instead** — ADR 0013 and the package's
    anonymisation plan both say ``auth_identities`` is hard-deleted, and this is
    the constraint that makes that mandatory rather than tidy. Nothing enforces
    it yet; the deletion path does not exist.
    """

    __tablename__ = "auth_identities"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[AuthProvider] = mapped_column(pg_enum(AuthProvider), nullable=False)
    provider_user_id: Mapped[str] = mapped_column(Text, nullable=False)

    linked_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    last_used_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    __table_args__ = (
        # Account linking on email collision is an insert against this
        # constraint, not a column update; unlinking is a DELETE, not a
        # five-field nullable update with no audit trail.
        Index("ix_auth_identities_provider_user", "provider", "provider_user_id", unique=True),
        Index("ix_auth_identities_user", "user_id"),
    )


class UserOnboarding(TimestampMixin, Base):
    """Onboarding progress. 1:1, keyed on ``user_id``.

    Legacy carried both ``registration completed`` (a date) and
    ``Registration completed (Y/N)`` (a flag). The flag is dropped as a
    duplicate — and unusually for this migration, that call is confirmed by data
    rather than assumed: the two agree on all 43 rows of the dev extract.

    ``last_step`` is text, not an integer, though the legacy values are "3".."6".
    Step identity belongs to the onboarding flow and will be renamed and
    reordered by product long before the column is; a number implies an ordering
    the database would then be asserting.
    """

    __tablename__ = "user_onboarding"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    # UNIQUE, for the same reason as user_profiles: this is the 1:1 invariant.
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    last_step: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))


class UserLanguage(TimestampMixin, Base):
    """Spoken languages. Attached to ``users``, not to a mentor profile.

    Mentee language matters for matching too, and legacy ``PersonalInfo`` already
    held ``Language`` / ``list-Language`` for every user. The duplicate copy on
    ``Mentor (front search).mentorLanguages`` is dropped — that duplication is
    what made the front-search table untrustworthy.

    **The M1c transform will violate this foreign key**, and it is worth knowing
    before it happens rather than after. Legacy stores comma-separated *display
    names* ("Abkhaz , Breton"). Against the M0 seed, ``Breton`` and ``Aymara``
    resolve; ``Abkhaz`` does not, because ISO 639-3 names it ``Abkhazian``; and
    ``Avestan`` is absent entirely, because M0 filtered to living languages. Two
    of four dev values fail, for two different reasons. The fix is a hand-checked
    alias map built from the Bubble option set, not a looser constraint here.
    """

    __tablename__ = "user_languages"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("uuid_generate_v7()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    language_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("languages.id", ondelete="RESTRICT"), nullable=False
    )
    proficiency: Mapped[LanguageProficiency] = mapped_column(
        pg_enum(LanguageProficiency),
        nullable=False,
        server_default=text("'fluent'"),
    )
    is_primary: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))

    __table_args__ = (
        Index("ix_user_languages_language", "language_id"),
        # The composite that used to be the primary key. Without it a user can
        # list the same language twice, which the old key made impossible for
        # free.
        Index(
            "ix_user_languages_user_language",
            "user_id",
            "language_id",
            unique=True,
        ),
        # At most one primary language per user. A plain unique on (user_id,
        # is_primary) would instead permit exactly one *non*-primary language,
        # which is the opposite of the rule.
        Index(
            "ix_user_languages_one_primary",
            "user_id",
            unique=True,
            postgresql_where=text("is_primary"),
        ),
    )
