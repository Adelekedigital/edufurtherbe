"""Legacy records into the shapes the identity tables expect.

Pure: dictionaries in, dataclasses out. No session, no SQLAlchemy, no I/O — the
loader in ``infra`` is what turns a ``UserRow`` into a database row. That split is
why every mapping decision here can be tested without a database, which is most
of what makes the transform reviewable.

**Every lookup raises on an unknown value rather than defaulting.** A migration
that silently substitutes ``mentee`` for a role it did not recognise produces
1,200 plausible rows and no error, and the mistake is only visible to somebody
who already suspects it. Failing means the unmapped value gets a name and a
decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.domain.bubble import CREATED_AT, MODIFIED_AT, normalise_list, parse_timestamp
from app.domain.enums import AdminRole, AuthProvider, PrimaryRole

# Legacy `👥Role` values. Written out rather than derived by lowercasing,
# because a transformation rule silently accepts whatever it is given: the day
# somebody adds "Coach" to the option set, `.lower()` yields a value the enum
# rejects at insert time, three tables deep into a load. An explicit mapping
# fails at the record, naming the value.
PRIMARY_ROLES: dict[str, PrimaryRole] = {
    "Mentee": PrimaryRole.MENTEE,
    "Mentor": PrimaryRole.MENTOR,
}

# `Slug` must satisfy the CHECK on `users`. Applied here so a bad value is
# reported per record before the load starts, rather than aborting it midway.
SLUG_PATTERN = re.compile(r"^[a-z0-9-]{1,60}$")

DEFAULT_TIMEZONE = "UTC"


class TransformError(ValueError):
    """A record could not be transformed without guessing."""

    def __init__(self, bubble_id: str, message: str) -> None:
        super().__init__(f"{bubble_id}: {message}")
        self.bubble_id = bubble_id


@dataclass(frozen=True, slots=True)
class UserRow:
    """One ``users`` row, before it meets a database.

    ``auth_id`` is deliberately absent rather than null-by-default: ADR 0014
    makes it the Supabase identifier, and nothing in the migration sets it. Every
    migrated user starts having never authenticated, which is what makes the load
    independent of Supabase.
    """

    legacy_bubble_id: str
    email: str
    primary_role: PrimaryRole
    timezone: str
    created_at: datetime
    updated_at: datetime
    email_verified_at: datetime | None = None
    first_name: str | None = None
    last_name: str | None = None
    slug: str | None = None
    last_active_at: datetime | None = None


def _timestamp(
    record: dict[str, Any], field: str, *, assume: tzinfo | None, bubble_id: str
) -> datetime | None:
    raw = record.get(field)
    if raw is None:
        return None
    try:
        return parse_timestamp(str(raw), assume=assume)
    except ValueError as exc:
        raise TransformError(bubble_id, f"{field}: {exc}") from exc


def _resolve_timezone(raw: Any, bubble_id: str) -> str:
    """Validate ``UserTimezonID`` against the real tz database.

    The runbook warns this field may hold non-IANA values and says to default to
    UTC and flag rather than guess. Defaulting is done here **only for an absent
    value**; a *present but unrecognised* one raises, because that is a mapping
    problem with a specific wrong answer, not a missing one.

    This is also why ``tzdata`` is a dependency: without it ``ZoneInfo`` rejects
    every name on Windows, so this check would fail 1,200 times on a developer's
    machine and pass in CI.
    """
    if raw is None or not str(raw).strip():
        return DEFAULT_TIMEZONE
    name = str(raw).strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise TransformError(bubble_id, f"UserTimezonID {name!r} is not an IANA zone") from exc
    return name


def to_user(record: dict[str, Any], *, export_timezone: tzinfo | None = None) -> UserRow:
    """One canonical record into a ``users`` row.

    ``export_timezone`` is passed through to timestamp parsing and is required
    only for records read from the Data-tab export, whose timestamps carry no
    offset. Records from the API need nothing.

    **``created_at`` and ``updated_at`` come from Bubble**, per settled decision
    #29 — Creation Date and Modified Date respectively. The loader must disable
    ``trg_set_updated_at`` for that to survive; nothing here can enforce it,
    which is why reconciliation compares the loaded values against the source.
    """
    bubble_id = str(record.get("bubble_id") or "")
    if not bubble_id:
        raise TransformError("<no id>", "record has no bubble_id")

    email = record.get("email")
    if not email:
        raise TransformError(bubble_id, "record has no email")

    role_raw = record.get("👥Role")
    if role_raw not in PRIMARY_ROLES:
        raise TransformError(bubble_id, f"unmapped role {role_raw!r}")

    created = _timestamp(record, CREATED_AT, assume=export_timezone, bubble_id=bubble_id)
    modified = _timestamp(record, MODIFIED_AT, assume=export_timezone, bubble_id=bubble_id)
    if created is None or modified is None:
        # Canonical names, set by the reader from whichever key the source
        # used — `Creation Date` in the export, `Created Date` in the API.
        raise TransformError(bubble_id, "created_at and modified_at are both required")

    slug = record.get("Slug")
    if slug is not None and not SLUG_PATTERN.match(str(slug)):
        raise TransformError(bubble_id, f"slug {slug!r} is not URL-safe")

    return UserRow(
        legacy_bubble_id=bubble_id,
        # Lowercased and trimmed on the way in, and this is now load-bearing
        # rather than tidy: the column is plain `text` with
        # `CHECK (email = lower(email))`, so a mixed-case value is **rejected**
        # rather than quietly accepted. That is deliberate (ADR 0016) —
        # normalisation belongs at the boundary and the constraint proves the
        # boundary did its job.
        email=str(email).strip().lower(),
        primary_role=PRIMARY_ROLES[str(role_raw)],
        timezone=_resolve_timezone(record.get("UserTimezonID"), bubble_id),
        created_at=created,
        updated_at=modified,
        email_verified_at=_timestamp(
            record, "email verified date", assume=export_timezone, bubble_id=bubble_id
        ),
        first_name=record.get("First Name"),
        last_name=record.get("Last Name"),
        slug=str(slug) if slug is not None else None,
        last_active_at=_timestamp(
            record, "Last Active", assume=export_timezone, bubble_id=bubble_id
        ),
    )


@dataclass(frozen=True, slots=True)
class TransformReport:
    """What a whole extract turned into, including what it could not.

    Errors are collected rather than raised, so one bad record does not hide the
    other 1,199. A load that stops at the first problem produces one fix per run,
    and the runbook's rehearsals are the wrong place to discover that serially.
    """

    rows: tuple[UserRow, ...]
    errors: tuple[str, ...]
    duplicate_emails: dict[str, tuple[str, ...]]

    @property
    def ok(self) -> bool:
        return not self.errors and not self.duplicate_emails


def transform_users(
    records: list[dict[str, Any]], *, export_timezone: tzinfo | None = None
) -> TransformReport:
    """Transform every record, and find the collisions no single record can show.

    **Duplicate emails are the reason this is a batch operation.** The partial
    unique index on ``users`` rejects the second one, so a duplicate is caught
    either way — but caught *there* it aborts a load halfway through, with some
    rows written and the operator holding a constraint name. Caught here it is a
    report before anything is touched, which is what the runbook asks for:
    "check for duplicates before loading — you want to find them in staging, not
    mid-load."
    """
    rows: list[UserRow] = []
    errors: list[str] = []

    for record in records:
        try:
            rows.append(to_user(record, export_timezone=export_timezone))
        except TransformError as exc:
            errors.append(str(exc))

    # Folded before comparing. Every row here has already been lowercased by
    # `to_user`, so this is belt-and-braces — but it costs nothing and it is the
    # half that would still be right if a future caller built a UserRow directly
    # rather than through the transform.
    by_email: dict[str, list[str]] = {}
    for row in rows:
        by_email.setdefault(row.email.lower(), []).append(row.legacy_bubble_id)

    return TransformReport(
        rows=tuple(rows),
        errors=tuple(errors),
        duplicate_emails={
            email: tuple(ids) for email, ids in sorted(by_email.items()) if len(ids) > 1
        },
    )


@dataclass(frozen=True, slots=True)
class ProfileRow:
    """One ``user_profiles`` row. Keyed on the *user*, not the profile."""

    legacy_bubble_id: str
    user_bubble_id: str
    created_at: datetime
    updated_at: datetime
    avatar_url: str | None = None
    banner_url: str | None = None
    about_me: str | None = None
    gender: str | None = None
    origin_country_name: str | None = None
    social_linkedin: str | None = None
    social_twitter: str | None = None
    social_youtube: str | None = None
    email_provider_contact_id: str | None = None
    language_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OnboardingRow:
    user_bubble_id: str
    last_step: str | None
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class IdentityRow:
    user_bubble_id: str
    provider: AuthProvider
    provider_user_id: str


@dataclass(frozen=True, slots=True)
class AdminGrantRow:
    user_bubble_id: str
    admin_role: AdminRole


# Legacy `Admin` option set. Only "Super Admin" appears in the dev extract; the
# other two are transcribed from the package's enum and have never been seen in
# data. They are listed anyway so a real occurrence maps rather than raises —
# and anything outside this table still raises, which is the point.
ADMIN_ROLES: dict[str, AdminRole] = {
    "Super Admin": AdminRole.SUPER_ADMIN,
    "Mentor Approval": AdminRole.MENTOR_APPROVAL,
    "Limited access": AdminRole.LIMITED_ACCESS,
}

# `Registration format`. **Email is deliberately absent**: an email registration
# has no external provider, so it produces no `auth_identities` row at all. A
# mapping that turned it into one would fail against the `auth_provider` enum,
# well into a load.
AUTH_PROVIDERS: dict[str, AuthProvider] = {
    "Google": AuthProvider.GOOGLE,
    "Linkedin": AuthProvider.LINKEDIN,
    "LinkedIn": AuthProvider.LINKEDIN,
}
NO_IDENTITY = "Email"

# The legacy field names, held as constants because two of them are hostile:
# one has a leading space and one a trailing space, and an exact literal is the
# only thing that reads either. A typo yields None from `.get`, which migrates a
# null rather than raising.
BANNER_FIELD = " Profile banner Image"
AVATAR_FIELD = "〽️User Profile image"
ADMIN_FIELD = "Admin \U0001f3a9"
ONBOARDING_COMPLETED_FIELD = "registration completed "


def to_profile(
    profile: dict[str, Any],
    *,
    user_bubble_id: str,
    user_record: dict[str, Any],
    export_timezone: tzinfo | None = None,
) -> ProfileRow:
    """A legacy ``PersonalInfo`` row, plus the two fields that live on ``User``.

    ``avatar_url`` and ``email_provider_contact_id`` come from the user record
    rather than the profile — the legacy model split them across two tables and
    the target model puts them together.

    **The country comes from ``OriginCountry(text)``, not ``Country of Origin``.**
    The canonical field mapping says the opposite and is wrong: the coded field
    is empty on every dev row and the text field carries every value. Following
    the document migrates zero countries and reports success. See the failure log.
    """
    bubble_id = str(profile.get("bubble_id") or "")
    if not bubble_id:
        raise TransformError(user_bubble_id, "profile has no bubble_id")

    created = _timestamp(profile, CREATED_AT, assume=export_timezone, bubble_id=bubble_id)
    modified = _timestamp(profile, MODIFIED_AT, assume=export_timezone, bubble_id=bubble_id)
    if created is None or modified is None:
        raise TransformError(bubble_id, "created_at and modified_at are both required")

    languages = normalise_list(profile.get("list-Language"))
    if single := profile.get("Language"):
        languages.append(str(single))

    return ProfileRow(
        legacy_bubble_id=bubble_id,
        user_bubble_id=user_bubble_id,
        created_at=created,
        updated_at=modified,
        avatar_url=user_record.get(AVATAR_FIELD),
        banner_url=profile.get(BANNER_FIELD),
        about_me=profile.get("About me"),
        gender=profile.get("Gender"),
        origin_country_name=profile.get("OriginCountry(text)"),
        social_linkedin=profile.get("Social Linkedin"),
        social_twitter=profile.get("Social Twitter"),
        social_youtube=profile.get("Social Youtube"),
        email_provider_contact_id=user_record.get("emailitContact_id"),
        # Deduplicated, order preserved. A user listing a language twice would
        # otherwise violate the composite unique index mid-load.
        language_names=tuple(dict.fromkeys(languages)),
    )


def to_onboarding(
    record: dict[str, Any], *, export_timezone: tzinfo | None = None
) -> OnboardingRow:
    """``User-last-onboarding-step`` and the completion date.

    ``Registration completed (Y/N)`` is dropped as a duplicate — and unusually
    for this migration that is confirmed by data rather than assumed: the flag
    and the date agree on all 43 rows of the dev extract.
    """
    bubble_id = str(record["bubble_id"])
    step = record.get("User-last-onboarding-step")
    return OnboardingRow(
        user_bubble_id=bubble_id,
        # Text, not an integer, though the values are "3".."6". Step identity
        # belongs to the onboarding flow, which product will renumber long before
        # the column changes; an integer implies an ordering the database would
        # then be asserting.
        last_step=str(step) if step is not None else None,
        completed_at=_timestamp(
            record, ONBOARDING_COMPLETED_FIELD, assume=export_timezone, bubble_id=bubble_id
        ),
    )


def to_identities(record: dict[str, Any]) -> list[IdentityRow]:
    """Linked OAuth providers, which only an API extract can supply.

    ``Registration format`` says *which* provider; ``provider_identities`` — set
    by the reader from the API's ``authentication`` object — says *who*. The
    export has the first and not the second, so it yields nothing here.

    That emptiness is reported by reconciliation rather than passed over: zero
    identity rows from an export means "wrong source", and zero from an API
    extract means "nobody linked a provider". They are not the same fact.
    """
    bubble_id = str(record["bubble_id"])
    fmt = record.get("Registration format")
    if fmt is None or str(fmt) == NO_IDENTITY:
        return []
    if str(fmt) not in AUTH_PROVIDERS:
        raise TransformError(bubble_id, f"unmapped registration format {fmt!r}")

    provider = AUTH_PROVIDERS[str(fmt)]
    subject = (record.get("provider_identities") or {}).get(provider.value)
    if not subject:
        return []

    return [IdentityRow(user_bubble_id=bubble_id, provider=provider, provider_user_id=str(subject))]


def to_admin_grant(record: dict[str, Any]) -> AdminGrantRow | None:
    """An ``admin_users`` row, only where the legacy option set is populated.

    ``granted_by`` stays null and ``granted_at`` takes the column default, which
    is the import time. Both are honest in different ways: legacy recorded that
    somebody was an admin, never who made them one or when. A synthetic actor or
    a back-dated grant would look like knowledge we do not have — and
    reconciliation says so rather than leaving the reader to notice.
    """
    bubble_id = str(record["bubble_id"])
    raw = record.get(ADMIN_FIELD)
    if raw is None or not str(raw).strip():
        return None
    if str(raw) not in ADMIN_ROLES:
        raise TransformError(bubble_id, f"unmapped admin role {raw!r}")
    return AdminGrantRow(user_bubble_id=bubble_id, admin_role=ADMIN_ROLES[str(raw)])


@dataclass(frozen=True, slots=True)
class IdentityPlan:
    """Everything one extract turns into, before any of it is written.

    Assembled as a whole rather than table by table because the satellites only
    make sense against the users they hang off: a profile whose owning user was
    refused must not be loaded, and that is knowable here and nowhere later.
    """

    users: tuple[UserRow, ...]
    profiles: tuple[ProfileRow, ...]
    onboarding: tuple[OnboardingRow, ...]
    identities: tuple[IdentityRow, ...]
    admin_grants: tuple[AdminGrantRow, ...]
    errors: tuple[str, ...]
    duplicate_emails: dict[str, tuple[str, ...]]
    orphaned_profiles: tuple[str, ...]
    source_carries_identities: bool

    @property
    def ok(self) -> bool:
        return not self.errors and not self.duplicate_emails

    def country_names(self) -> set[str]:
        return {p.origin_country_name for p in self.profiles if p.origin_country_name}

    def language_names(self) -> set[str]:
        return {name for p in self.profiles for name in p.language_names}


PROFILE_LINK_FIELD = "\U0001f464Personal Info"


def plan_identity(
    user_records: list[dict[str, Any]],
    profile_records: list[dict[str, Any]],
    *,
    export_timezone: tzinfo | None = None,
) -> IdentityPlan:
    """Transform a whole extract into everything the identity phase will write.

    **Profiles are joined from the user side**, through ``User.Personal Info``.
    The legacy ``PersonalInfo`` row carries no reference back to its owner — only
    a ``Creator`` email — so the link exists in exactly one direction and a
    profile nobody points at cannot be attributed to anyone. Those are reported
    as orphans rather than guessed at by matching on the creator's address.
    """
    report = transform_users(user_records, export_timezone=export_timezone)
    users_by_id = {row.legacy_bubble_id: row for row in report.rows}
    raw_by_id = {str(r.get("bubble_id")): r for r in user_records}

    profiles_by_id = {str(p.get("bubble_id")): p for p in profile_records}
    linked: dict[str, str] = {}
    for record in user_records:
        profile_id = record.get(PROFILE_LINK_FIELD)
        if profile_id and str(record.get("bubble_id")) in users_by_id:
            linked[str(profile_id)] = str(record["bubble_id"])

    errors = list(report.errors)
    profiles: list[ProfileRow] = []
    for profile_id, user_bubble_id in sorted(linked.items()):
        source = profiles_by_id.get(profile_id)
        if source is None:
            # The user points at a profile the extract does not contain. Usually
            # means the two files were pulled at different times.
            errors.append(f"{user_bubble_id}: profile {profile_id} is not in the extract")
            continue
        try:
            profiles.append(
                to_profile(
                    source,
                    user_bubble_id=user_bubble_id,
                    user_record=raw_by_id[user_bubble_id],
                    export_timezone=export_timezone,
                )
            )
        except TransformError as exc:
            errors.append(str(exc))

    onboarding: list[OnboardingRow] = []
    identities: list[IdentityRow] = []
    admin_grants: list[AdminGrantRow] = []
    for bubble_id in users_by_id:
        record = raw_by_id[bubble_id]
        try:
            onboarding.append(to_onboarding(record, export_timezone=export_timezone))
            identities.extend(to_identities(record))
            if (grant := to_admin_grant(record)) is not None:
                admin_grants.append(grant)
        except TransformError as exc:
            errors.append(str(exc))

    return IdentityPlan(
        users=report.rows,
        profiles=tuple(profiles),
        onboarding=tuple(onboarding),
        identities=tuple(identities),
        admin_grants=tuple(admin_grants),
        errors=tuple(errors),
        duplicate_emails=report.duplicate_emails,
        orphaned_profiles=tuple(sorted(set(profiles_by_id) - set(linked))),
        # Distinguishes "no OAuth users" from "wrong source". Only an API extract
        # carries `provider_identities`; the export has `Registration format` and
        # no subject id, so it can never produce an identity row.
        source_carries_identities=any(r.get("provider_identities") for r in user_records),
    )
