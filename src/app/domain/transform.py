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

from app.domain.bubble import parse_timestamp
from app.domain.enums import PrimaryRole

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

    created = _timestamp(record, "created_at", assume=export_timezone, bubble_id=bubble_id)
    modified = _timestamp(record, "modified_at", assume=export_timezone, bubble_id=bubble_id)
    if created is None or modified is None:
        # Canonical names, set by the reader from whichever key the source
        # used — `Creation Date` in the export, `Created Date` in the API.
        raise TransformError(bubble_id, "created_at and modified_at are both required")

    slug = record.get("Slug")
    if slug is not None and not SLUG_PATTERN.match(str(slug)):
        raise TransformError(bubble_id, f"slug {slug!r} is not URL-safe")

    return UserRow(
        legacy_bubble_id=bubble_id,
        # Lowercased and trimmed on the way in. The column is citext so a
        # comparison would match either way, but the partial unique index is on
        # the stored value, and two rows differing only in case would be one
        # collision the index reports and a human cannot see.
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

    # Compared case-insensitively because `users.email` is citext: `A@x.com` and
    # `a@x.com` are one address to the index, and comparing the raw strings here
    # would report no duplicate and then fail at insert.
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
