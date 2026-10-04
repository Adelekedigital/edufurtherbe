"""Intake files: stored on upload, linked at booking, read by three people, swept.

**Every read and write is scoped in its own statement** (non-negotiable #5). An
upload is usable only by its uploader and only while it answers nothing; the
link that marks it used is an `UPDATE` carrying both conditions, so two bookings
racing for one file cannot both win — the second re-reads a row that is no
longer unlinked and matches nothing. A download finds the row only if the
caller uploaded it, is the mentor of the session it answers, or is an admin.

**The object and the row are not one transaction**, because Storage is not the
database. An upload writes the row, then the object, then commits, so a failed
object write leaves no row. The sweep marks rows gone before it deletes their
objects, so a row that says "live" always has its object; a crash between the
two leaves a marked row whose object the next run deletes again (a missing
object counts as deleted).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, exists, func, insert, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, RateLimitedError, ValidationError
from app.domain.enums import IntakeFileType
from app.domain.intake_files import (
    MAX_PENDING_UPLOADS,
    UPLOAD_RATE_WINDOW,
    retention_cutoff,
    storage_key,
    unused_cutoff,
    upload_retry_after,
)
from app.infra.db.models.intake import IntakeFile
from app.infra.db.models.sessions import Session
from app.infra.db.models.user import User
from app.infra.db.predicates import LIVE
from app.infra.storage.supabase import StorageError, SupabaseStorage

logger = logging.getLogger(__name__)

__all__ = [
    "link_files",
    "readable_file",
    "store_intake_file",
    "sweep_counts",
    "sweep_intake_files",
    "usable_file_ids",
]

#: How many marked files one sweep removes. The rest wait for the next run.
PURGE_BATCH = 500


def _live_unlinked() -> Any:
    """An upload that is live and answers nothing yet — anyone's.

    **The one statement of "still usable for a booking"**, which the sweep's
    "abandoned" is built on too. The two must stay each other's complement: if
    only one gained a condition, the sweep would delete a file a booking can
    still link, or leave one nothing can.
    """
    return and_(IntakeFile.session_id.is_(None), IntakeFile.deleted_at.is_(None))


def _unlinked(uploader_id: UUID) -> Any:
    """An upload its owner may still answer with: theirs, live, answering nothing."""
    return and_(IntakeFile.uploader_id == uploader_id, _live_unlinked())


def sweep_counts(
    *, unused: int = 0, expired: int = 0, departed: int = 0, purged: int = 0, failed: int = 0
) -> dict[str, int]:
    """What a sweep reports — the one place its keys are named."""
    return {
        "unused": unused,
        "expired": expired,
        "departed": departed,
        "purged": purged,
        "failed": failed,
    }


async def _within_upload_rate(session: AsyncSession, uploader_id: UUID, limit: int) -> None:
    """Refuse an upload past `limit` in the last hour, saying when to retry.

    **Counted on the database's clock**, the one `created_at` was written with,
    so app and database clocks can't disagree about the window. Every upload in
    the window still has its row: only unlinked uploads are ever deleted, and
    not until a day has passed. `ix_intake_files_uploader` serves the count.
    """
    count, oldest, now = (
        await session.execute(
            select(func.count(), func.min(IntakeFile.created_at), func.now()).where(
                IntakeFile.uploader_id == uploader_id,
                IntakeFile.created_at > func.now() - literal(UPLOAD_RATE_WINDOW),
            )
        )
    ).one()
    if count >= limit:
        raise RateLimitedError(
            f"you have uploaded {limit} files in the last hour; try again later",
            retry_after_seconds=upload_retry_after(oldest, now),
        )


async def store_intake_file(
    session: AsyncSession,
    storage: SupabaseStorage,
    *,
    uploader_id: UUID,
    filename: str,
    payload: bytes,
    kind: IntakeFileType,
    uploads_per_hour: int,
) -> dict[str, Any]:
    """Write the row, then the object, then commit; the new file's description.

    **Pending uploads are bounded per person, then upload rate is** (#281).
    The pending cap bounds storage but not rate (upload, book, upload again);
    the hourly count bounds that. Pending is checked first because its refusal
    tells the person what to do (book with them), where the rate's only says
    when. Both counts race, and a burst can pass them by a few: the bounds are
    on abuse, not on a number anybody relies on.
    """
    pending = (
        await session.execute(select(func.count()).where(_unlinked(uploader_id)))
    ).scalar_one()
    if pending >= MAX_PENDING_UPLOADS:
        raise ConflictError(
            f"you have {MAX_PENDING_UPLOADS} uploads not yet used in a booking; "
            "book with them, or wait a day for them to expire"
        )
    await _within_upload_rate(session, uploader_id, uploads_per_hour)
    # A random object name, never the row id — ids are the database's to
    # assign (ADR 0015) — and never the uploader's filename.
    key = storage_key(uploader_id, uuid4())
    file_id = (
        await session.execute(
            insert(IntakeFile)
            .values(
                uploader_id=uploader_id,
                storage_key=key,
                filename=filename,
                size_bytes=len(payload),
                content_type=kind,
            )
            .returning(IntakeFile.id)
        )
    ).scalar_one()
    await asyncio.to_thread(storage.upload, key, payload, kind.value)
    try:
        await session.commit()
    except Exception:
        # The object landed and its row did not: take the object back rather
        # than leave personal data with nothing that would ever expire it.
        await session.rollback()
        try:
            await asyncio.to_thread(storage.delete, key)
        except StorageError:
            logger.warning("intake file orphaned after a failed commit")
        raise
    return {"file_id": file_id, "filename": filename, "size": len(payload), "content_type": kind}


async def usable_file_ids(
    session: AsyncSession, uploader_id: UUID, file_ids: list[UUID]
) -> frozenset[UUID]:
    """Which of ``file_ids`` this person may answer with right now."""
    if not file_ids:
        return frozenset()
    rows = await session.execute(
        select(IntakeFile.id).where(IntakeFile.id.in_(file_ids), _unlinked(uploader_id))
    )
    return frozenset(rows.scalars())


async def link_files(
    session: AsyncSession, *, session_id: UUID, uploader_id: UUID, file_ids: list[UUID]
) -> dict[UUID, str]:
    """Mark these uploads as answering this booking; each one's storage key.

    **The guard is the `WHERE`**, not the check before it: a file another
    booking linked a moment ago no longer matches, and the whole booking is
    refused rather than stored without the answer. Does not commit.
    """
    if not file_ids:
        return {}
    rows = await session.execute(
        update(IntakeFile)
        .where(IntakeFile.id.in_(file_ids), _unlinked(uploader_id))
        .values(session_id=session_id)
        .returning(IntakeFile.id, IntakeFile.storage_key)
    )
    linked = {row.id: row.storage_key for row in rows}
    if len(linked) != len(set(file_ids)):
        raise ValidationError("a file in this booking was just used or removed; upload it again")
    return linked


async def readable_file(
    session: AsyncSession, *, file_id: UUID, caller_id: UUID, caller_is_admin: bool
) -> dict[str, Any] | None:
    """The live file, if this caller is one of its three readers; else ``None``.

    The uploader, the mentor of the session it answers — so not before it
    answers one — and any live admin. Everyone else, and a file past retention,
    is the same ``None``, which the route answers as 404.
    """
    row = (
        await session.execute(
            select(
                IntakeFile.storage_key,
                IntakeFile.filename,
                IntakeFile.content_type,
                IntakeFile.size_bytes,
            )
            .select_from(IntakeFile)
            .outerjoin(Session, Session.id == IntakeFile.session_id)
            .where(
                IntakeFile.id == file_id,
                IntakeFile.deleted_at.is_(None),
                or_(
                    IntakeFile.uploader_id == caller_id,
                    Session.mentor_id == caller_id,
                    literal(caller_is_admin),
                ),
            )
        )
    ).first()
    return dict(row._mapping) if row else None


async def sweep_intake_files(
    session: AsyncSession,
    storage: SupabaseStorage,
    *,
    now: dt.datetime,
    retention_days: int | None,
    unused_hours: int,
    dry_run: bool,
) -> dict[str, int]:
    """Expire abandoned uploads and files past retention, then remove objects.

    Counts: ``unused`` uploads never used in a booking and older than
    ``unused_hours``; ``expired`` files older than ``retention_days`` (none when
    it is unset); ``departed`` files whose uploader has deleted their account,
    whatever the retention (#280); ``purged`` objects removed; ``failed``
    removals left for the next run. A dry run counts the first three and
    changes nothing. Each file is counted once, in the first of those it meets.

    **Departed is the uploader's account only**: a deleted mentor does not take
    a mentee's file. There is no account-deletion hook to call, so the daily
    sweep is where a deletion reaches the bucket, at most a day later.
    """
    abandoned = and_(_live_unlinked(), IntakeFile.created_at < unused_cutoff(now, unused_hours))
    cutoff = retention_cutoff(now, retention_days)
    past_retention = (
        and_(IntakeFile.deleted_at.is_(None), IntakeFile.created_at < cutoff)
        if cutoff is not None
        else None
    )
    departed = and_(
        IntakeFile.deleted_at.is_(None),
        ~exists(select(User.id).where(User.id == IntakeFile.uploader_id, LIVE)),
    )
    if dry_run:
        unused = (await session.execute(select(func.count()).where(abandoned))).scalar_one()
        expired = 0
        earlier = ~abandoned
        if past_retention is not None:
            expired = (
                await session.execute(select(func.count()).where(past_retention, ~abandoned))
            ).scalar_one()
            earlier = and_(earlier, ~past_retention)
        gone = (await session.execute(select(func.count()).where(departed, earlier))).scalar_one()
        await session.rollback()
        return sweep_counts(unused=unused, expired=expired, departed=gone)

    unused = len(
        (
            await session.execute(
                update(IntakeFile).where(abandoned).values(deleted_at=now).returning(IntakeFile.id)
            )
        ).all()
    )
    expired = 0
    if past_retention is not None:
        expired = len(
            (
                await session.execute(
                    update(IntakeFile)
                    .where(past_retention)
                    .values(deleted_at=now)
                    .returning(IntakeFile.id)
                )
            ).all()
        )
    # After the two above, so each file is counted in the first that takes it.
    gone = len(
        (
            await session.execute(
                update(IntakeFile).where(departed).values(deleted_at=now).returning(IntakeFile.id)
            )
        ).all()
    )
    await session.commit()

    purged = failed = 0
    marked = (
        await session.execute(
            select(IntakeFile.id, IntakeFile.storage_key, IntakeFile.session_id)
            .where(IntakeFile.deleted_at.is_not(None), IntakeFile.purged_at.is_(None))
            .order_by(IntakeFile.deleted_at)
            .limit(PURGE_BATCH)
        )
    ).all()
    for row in marked:
        try:
            await asyncio.to_thread(storage.delete, row.storage_key)
        except StorageError:
            failed += 1
            continue
        purged += 1
        if row.session_id is None:
            # Nothing names an upload that never answered anything; the row
            # goes with its object.
            await session.execute(delete(IntakeFile).where(IntakeFile.id == row.id))
        else:
            await session.execute(
                update(IntakeFile).where(IntakeFile.id == row.id).values(purged_at=now)
            )
    await session.commit()
    if failed:
        logger.warning("intake file sweep left objects for the next run", extra={"failed": failed})
    return sweep_counts(unused=unused, expired=expired, departed=gone, purged=purged, failed=failed)
