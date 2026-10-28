"""What every private upload shares: the per-person bounds, and how the row
and the object are written together (intake files and message files,
decision 302; non-negotiable #8).

**Each surface counts its own uploads against the same numbers.** An intake
upload and a chat file are different tables, so each passes its own
uploader's rows in; the bound (`MAX_PENDING_UPLOADS`, the hourly
`INTAKE_UPLOADS_PER_HOUR`) and the refusal are one.

**The object and the row are not one transaction**, because Storage is not the
database. The row is written first, then the object, then the commit, so a
failed object write leaves no row; a failed commit takes the object back.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, RateLimitedError
from app.domain.enums import IntakeFileType
from app.domain.intake_files import MAX_PENDING_UPLOADS, UPLOAD_RATE_WINDOW, upload_retry_after
from app.infra.storage.supabase import StorageError, SupabaseStorage

logger = logging.getLogger(__name__)

__all__ = ["commit_with_object", "refuse_pending", "within_upload_rate"]


async def refuse_pending(session: AsyncSession, unused: Any, message: str) -> None:
    """Refuse a new upload while the person already holds `MAX_PENDING_UPLOADS`
    unused ones (``unused``: their rows that nothing uses yet)."""
    pending = (await session.execute(select(func.count()).where(unused))).scalar_one()
    if pending >= MAX_PENDING_UPLOADS:
        raise ConflictError(message)


async def within_upload_rate(
    session: AsyncSession, *, mine: Any, created_at: Any, limit: int
) -> None:
    """Refuse an upload past `limit` in the last hour, saying when to retry (#281).

    ``mine`` is the uploader's rows, deleted or not, and ``created_at`` their
    upload time. **Counted on the database's clock**, the one `created_at` was
    written with, so app and database clocks can't disagree about the window.

    **`Retry-After` waits for enough uploads to leave the window, not just the
    oldest.** The count can exceed the limit (a concurrent burst, or a lowered
    setting), and the next upload is allowed once it has fallen to `limit - 1`,
    which is when the `count - limit + 1`-th oldest upload ages out.
    """
    in_window = (mine, created_at > func.now() - literal(UPLOAD_RATE_WINDOW))
    count, now = (await session.execute(select(func.count(), func.now()).where(*in_window))).one()
    if count < limit:
        return
    freeing = (
        await session.execute(
            select(created_at).where(*in_window).order_by(created_at).offset(count - limit).limit(1)
        )
    ).scalar()
    raise RateLimitedError(
        f"you have uploaded {limit} files in the last hour; try again later",
        # None only if rows left the window between the two reads: retry soon.
        retry_after_seconds=1 if freeing is None else upload_retry_after(freeing, now),
    )


async def commit_with_object(
    session: AsyncSession,
    storage: SupabaseStorage,
    *,
    key: str,
    payload: bytes,
    kind: IntakeFileType,
) -> None:
    """Write the object for a row already inserted, then commit both.

    A failed upload raises before the commit, so no row is kept. A failed
    commit takes the object back rather than leave personal data with nothing
    that would ever expire it.
    """
    await asyncio.to_thread(storage.upload, key, payload, kind.value)
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        try:
            await asyncio.to_thread(storage.delete, key)
        except StorageError:
            logger.warning("private upload orphaned after a failed commit")
        raise
