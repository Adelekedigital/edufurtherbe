"""Purging self-deleted accounts once their 30 days pass, and restoring one before.

`DELETE /me` stamps `deleted_at` and `deletion_purge_at` (`account_deletion.py`).
Until the purge date a super admin may restore the account; after it, the daily
job removes what the person put on the platform and anonymises the account row.

WHAT GOES AND WHAT STAYS
========================
**Goes**: the PRD's list (profile, goals, education, languages, avatar files,
the Supabase account), the contact a migrated account has at Loops, and the rest
that is only theirs and that nothing kept points at: sign-in identities, calendar
tokens, awards, onboarding, interest, writing-assist counts, idempotency keys, a
mentor's availability, listings and personal meeting links.

**Stays, unnamed**: sessions, reviews, the credit ledger and the audit logs, as
the privacy policy keeps them for 12 months. They are anonymised by the account
row losing its name, email, slug and sign-in: every read already shows a deleted
party as unnamed. A mentor profile and offerings stay too, because sessions and
the status log point at them, so what the mentor wrote there is cleared.

**Done is `deletion_purge_at` back to null**: a purged account is then a deleted
row with no purge date, which the job never picks up again and restore refuses.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import delete, not_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ConflictError
from app.infra.auth.admin import AdminApiError
from app.infra.clients.loops_contacts import ContactsError
from app.infra.db.models.availability import (
    AvailabilityException,
    AvailabilityRule,
    CalendarConnection,
    MentorNextAvailability,
)
from app.infra.db.models.education import EducationEntry
from app.infra.db.models.legal import UserLegalConsent
from app.infra.db.models.mentoring import (
    FeaturedMentor,
    MenteeGoal,
    MentorConferencingOption,
    MentorProfile,
    MentorServiceOffering,
)
from app.infra.db.models.platform import FeatureInterest, IdempotencyKey
from app.infra.db.models.scholarships import UserAward
from app.infra.db.models.sessions import SessionType
from app.infra.db.models.user import AuthIdentity, User, UserLanguage, UserOnboarding, UserProfile
from app.infra.db.models.writing_assist import WritingAssistRequest
from app.infra.db.predicates import LIVE

logger = logging.getLogger(__name__)

#: How many accounts one run purges at most. A day's deletions are a handful;
#: the cap only stops a backlog becoming one run that outlives its timeout.
PURGE_BATCH = 100

#: Tables holding only this person's data, by the column naming them. Deleted
#: outright; `mentee_goals` takes its countries, needs and schools with it.
#: Conferencing options go after the offerings' references to them are cleared.
_OWN_ROWS: tuple[tuple[Any, Any], ...] = (
    (UserProfile, UserProfile.user_id),
    (AuthIdentity, AuthIdentity.user_id),
    (UserOnboarding, UserOnboarding.user_id),
    (UserLanguage, UserLanguage.user_id),
    (EducationEntry, EducationEntry.user_id),
    (UserAward, UserAward.user_id),
    (MenteeGoal, MenteeGoal.user_id),
    (CalendarConnection, CalendarConnection.user_id),
    (WritingAssistRequest, WritingAssistRequest.user_id),
    (FeatureInterest, FeatureInterest.user_id),
    (IdempotencyKey, IdempotencyKey.user_id),
    (AvailabilityRule, AvailabilityRule.mentor_user_id),
    (AvailabilityException, AvailabilityException.mentor_user_id),
    (MentorServiceOffering, MentorServiceOffering.mentor_user_id),
    (FeaturedMentor, FeaturedMentor.mentor_user_id),
    (MentorNextAvailability, MentorNextAvailability.mentor_user_id),
    (MentorConferencingOption, MentorConferencingOption.user_id),
)


class AuthAccounts(Protocol):
    """The Supabase Admin API, as far as the purge needs it."""

    def delete_user(self, auth_id: UUID) -> None: ...


class Contacts(Protocol):
    """Loops contacts, as far as the purge needs them."""

    def delete(self, email: str) -> None: ...


class Images(Protocol):
    """The public image bucket, as far as the purge needs it."""

    def drop_url(self, url: str) -> bool: ...


def purge_counts(*, due: int = 0, purged: int = 0, failed: int = 0) -> dict[str, int]:
    """The job's result, one shape for every path that reports it."""
    return {"due": due, "purged": purged, "failed": failed}


def _purge_passed(now: dt.datetime) -> Any:
    """**The one boundary** between restorable and purgeable: purge on or after
    the date, restore strictly before it. Both read this, so no instant is both."""
    return User.deletion_purge_at <= now


def _due(now: dt.datetime) -> list[Any]:
    """A self-deleted account past its purge date."""
    return [User.deleted_at.is_not(None), _purge_passed(now)]


def _restorable(now: dt.datetime) -> list[Any]:
    """A self-deleted account still inside its window."""
    return [
        User.deleted_at.is_not(None),
        User.deletion_purge_at.is_not(None),
        not_(_purge_passed(now)),
    ]


def _tombstone(user_id: UUID) -> str:
    """An address that satisfies `users`' constraints and reaches nobody:
    `.invalid` is reserved and never resolves (RFC 2606)."""
    return f"deleted-{user_id}@deleted.invalid"


async def purge_account(
    session: AsyncSession, user_id: UUID, *, now: dt.datetime
) -> list[str] | None:
    """Purge one account, or ``None`` if it is no longer due. Returns the image
    URLs to drop **after** the caller commits. Does not commit."""
    locked = await session.execute(
        select(User.id).where(User.id == user_id, *_due(now)).with_for_update()
    )
    if locked.first() is None:
        return None

    images = (
        await session.execute(
            select(UserProfile.avatar_url, UserProfile.banner_url).where(
                UserProfile.user_id == user_id
            )
        )
    ).first()
    # The offerings stay for their sessions: hidden, without what the mentor
    # wrote, and no longer pointing at the meeting links deleted below.
    await session.execute(
        update(SessionType)
        .where(SessionType.mentor_user_id == user_id)
        .values(
            description=None,
            conferencing_option_id=None,
            is_active=False,
            is_featured=False,
            deleted_at=now,
        )
    )
    for model, column in _OWN_ROWS:
        await session.execute(delete(model).where(column == user_id))
    # Consent stays as evidence of what was agreed; where from does not.
    await session.execute(
        update(UserLegalConsent).where(UserLegalConsent.user_id == user_id).values(ip_address=None)
    )
    await session.execute(
        update(MentorProfile)
        .where(MentorProfile.user_id == user_id)
        .values(headline=None, primary_study_program=None, deleted_at=now)
    )
    await session.execute(
        update(User)
        .where(User.id == user_id)
        .values(
            email=_tombstone(user_id),
            email_verified_at=None,
            first_name=None,
            last_name=None,
            slug=None,
            auth_id=None,
            last_active_at=None,
            deletion_purge_at=None,
        )
    )
    return [url for url in (images or ()) if url]


async def _lock_due(
    session: AsyncSession, user_id: UUID, *, now: dt.datetime
) -> tuple[UUID | None, str] | None:
    """Lock the account row if it is still due, and read what the providers need
    (`auth_id`, the real address). ``None`` once it is not: restored, or purged
    by another run. Restore's own clock may differ from this job's, so the lock,
    not the shared boundary alone, is what keeps a restored sign-in safe."""
    row = (
        await session.execute(
            select(User.auth_id, User.email).where(User.id == user_id, *_due(now)).with_for_update()
        )
    ).first()
    return None if row is None else (row.auth_id, row.email)


async def _loops_contact(session: AsyncSession, user_id: UUID, email: str) -> str | None:
    """The address to delete at Loops, unless a live account now holds it too:
    the contact would then be theirs as much as this one's."""
    shared = await session.execute(
        select(User.id).where(User.email == email, User.id != user_id, LIVE).limit(1)
    )
    return None if shared.first() is not None else email


async def purge_due_accounts(
    factory: async_sessionmaker[AsyncSession],
    *,
    admin: AuthAccounts,
    contacts: Contacts | None,
    storage: Images,
    now: dt.datetime,
    dry_run: bool = False,
) -> dict[str, int]:
    """Purge every due account, one transaction each. The daily job.

    **The providers first, then ours.** The Loops contact is found by the real
    address and the Supabase sign-in by `auth_id`, both of which our purge
    clears, so if either provider refuses, our row is left as it was and the
    next run tries again. Already gone counts as removed at both, so a run that
    died between a provider and our commit simply finishes next time.

    **One account's failure is that account's.** A database error rolls its
    transaction back, counts it failed and moves on: the earliest-due account is
    always taken first, so letting it end the run would stop every run behind it.

    Images go after the commit: dropped first, a failed commit would leave a
    profile pointing at nothing. `drop_url` never raises. ``contacts`` is
    ``None`` where Loops is not configured, and the contact is then kept.
    """
    async with factory() as session:
        due = (
            await session.scalars(
                select(User.id)
                .where(*_due(now))
                .order_by(User.deletion_purge_at, User.id)
                .limit(PURGE_BATCH)
            )
        ).all()
    counts = purge_counts(due=len(due))
    if dry_run:
        return counts

    for user_id in due:
        try:
            # **Locked before either provider is called**, and held through our
            # commit: an account restored after it was listed is skipped whole,
            # sign-in and contact included, and a restore arriving now waits.
            async with factory() as session:
                held = await _lock_due(session, user_id, now=now)
                if held is None:
                    continue
                auth_id, email = held
                if contacts is not None:
                    address = await _loops_contact(session, user_id, email)
                    if address is not None:
                        await asyncio.to_thread(contacts.delete, address)
                if auth_id is not None:
                    await asyncio.to_thread(admin.delete_user, auth_id)
                images = await purge_account(session, user_id, now=now)
                if images is None:  # pragma: no cover - the lock above holds the row
                    continue
                await session.commit()
        except (AdminApiError, ContactsError, SQLAlchemyError) as exc:
            logger.warning("purge of account %s deferred: %s", user_id, type(exc).__name__)
            counts["failed"] += 1
            continue
        counts["purged"] += 1
        logger.info("account %s purged", user_id)
        for url in images:
            await asyncio.to_thread(storage.drop_url, url)
    return counts


async def restore_account(session: AsyncSession, user_id: UUID, *, now: dt.datetime) -> bool:
    """Undo a self-deletion while its window is open. ``False`` once the purge
    date has passed, or for an account that was never self-deleted.

    **Only the account comes back.** Sessions it ended stay ended and its expired
    credits stay expired; a mentor stays unlisted until an admin lists them
    (settled decision 274). Raises :class:`ConflictError` when the address or
    handle has since been taken by a live account. Does not commit.
    """
    try:
        restored = await session.execute(
            update(User)
            .where(User.id == user_id, *_restorable(now))
            .values(deleted_at=None, deletion_purge_at=None)
            .returning(User.id)
        )
    except IntegrityError as exc:
        await session.rollback()
        raise ConflictError("this account's email or handle now belongs to a live account") from exc
    return restored.first() is not None
