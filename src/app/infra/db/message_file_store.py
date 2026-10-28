"""Message files: uploaded first, attached on send, read by the chat, swept
when never sent (decision 302; the Messages PRD).

**Every read and write is scoped in its own statement** (non-negotiable #5).
An upload is attachable only by its uploader while it is unsent, and the
attach is an `UPDATE` carrying both conditions, so two sends racing for one
file cannot both win: the second waits on the row, re-reads it as sent and
matches nothing. A download finds the row only for the uploader before it is
sent, or for either person in the chat it was sent in while they are still
connected (M0) and the message is not deleted.

**A swept upload has no row**: `message_attachments` keeps no tombstone, so
"swept" and "never existed" are the same miss everywhere.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, case, delete, exists, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import FileUnavailableError
from app.domain.chats import file_storage_key
from app.domain.enums import IntakeFileType
from app.domain.intake_files import MAX_PENDING_UPLOADS, unused_cutoff
from app.infra.db.chat_store import FILE_COLUMNS, connections
from app.infra.db.models.messaging import Conversation, Message, MessageAttachment
from app.infra.db.uploads import commit_with_object, refuse_pending, within_upload_rate
from app.infra.storage.supabase import StorageError, SupabaseStorage

logger = logging.getLogger(__name__)

__all__ = [
    "attach_file",
    "message_sweep_counts",
    "readable_message_file",
    "store_message_file",
    "sweep_message_files",
]

#: How many unsent uploads one sweep removes. The rest wait for the next run.
SWEEP_BATCH = 500


def _unsent() -> Any:
    """**The one statement of "not sent yet"**, which the attach, the pending
    cap and the sweep all read: the sweep removes exactly what a send could
    still attach once it is old enough, never a sent file."""
    return MessageAttachment.message_id.is_(None)


async def store_message_file(
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

    The intake bounds on this table's own rows (`uploads`): at most
    `MAX_PENDING_UPLOADS` unsent, then the hourly count, which
    `ix_message_attachments_uploader` serves. A chat file does not spend the
    intake budget, nor intake the chat's.
    """
    mine = MessageAttachment.uploaded_by == uploader_id
    await refuse_pending(
        session,
        and_(mine, _unsent()),
        f"you have {MAX_PENDING_UPLOADS} files not yet sent in a message; send "
        "them or wait a day for them to expire",
    )
    await within_upload_rate(
        session, mine=mine, created_at=MessageAttachment.created_at, limit=uploads_per_hour
    )
    # A random object name, never the row id (ADR 0015) nor the filename.
    key = file_storage_key(uploader_id, uuid4())
    file_id = (
        await session.execute(
            insert(MessageAttachment)
            .values(
                uploaded_by=uploader_id,
                storage_key=key,
                file_name=filename,
                mime_type=kind,
                size_bytes=len(payload),
            )
            .returning(MessageAttachment.id)
        )
    ).scalar_one()
    await commit_with_object(session, storage, key=key, payload=payload, kind=kind)
    return {"file_id": file_id, "filename": filename, "size": len(payload), "content_type": kind}


async def attach_file(
    session: AsyncSession, *, file_id: UUID, sender_id: UUID, message_id: UUID
) -> dict[str, Any]:
    """Attach the sender's unsent upload to their new message; its fields.

    **The guard is the `WHERE`**: somebody else's file, one already sent and
    one swept all match nothing, and :class:`FileUnavailableError` (a `422`
    at `/file_id`) rolls the whole send back. Does not commit.
    """
    attached = (
        (
            await session.execute(
                update(MessageAttachment)
                .where(
                    MessageAttachment.id == file_id,
                    MessageAttachment.uploaded_by == sender_id,
                    _unsent(),
                )
                .values(message_id=message_id)
                .returning(*FILE_COLUMNS)
            )
        )
        .mappings()
        .first()
    )
    if attached is None:
        raise FileUnavailableError(
            "that file is not yours to send, was already sent, or has expired; upload it again",
            field_errors=(("/file_id", "not an unsent upload of yours"),),
        )
    return dict(attached)


async def readable_message_file(
    session: AsyncSession, *, file_id: UUID, caller_id: UUID
) -> dict[str, Any] | None:
    """The file, if this caller may read it; else ``None`` (the route's `404`).

    The uploader while it is unsent; once sent, either person in its chat
    while the two are connected (M0, `chat_store.connections`, as the
    thread is), and only while its message is not deleted. Anyone else, a
    missing id and a swept upload are the same ``None``.
    """
    other = case(
        (Conversation.mentor_user_id == caller_id, Conversation.mentee_user_id),
        else_=Conversation.mentor_user_id,
    )
    link = connections(caller_id)
    row = (
        (
            await session.execute(
                select(
                    MessageAttachment.storage_key,
                    MessageAttachment.file_name,
                    MessageAttachment.mime_type,
                )
                .select_from(MessageAttachment)
                .outerjoin(Message, Message.id == MessageAttachment.message_id)
                .outerjoin(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    MessageAttachment.id == file_id,
                    or_(
                        and_(_unsent(), MessageAttachment.uploaded_by == caller_id),
                        and_(
                            Message.deleted_at.is_(None),
                            or_(
                                Conversation.mentor_user_id == caller_id,
                                Conversation.mentee_user_id == caller_id,
                            ),
                            exists(select(link.c.other_id).where(link.c.other_id == other)),
                        ),
                    ),
                )
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


def message_sweep_counts(*, swept: int = 0, failed: int = 0) -> dict[str, int]:
    """What the message-file step reports, prefixed apart from intake's keys."""
    return {"message_files_swept": swept, "message_files_failed": failed}


async def sweep_message_files(
    session: AsyncSession,
    storage: SupabaseStorage,
    *,
    now: dt.datetime,
    unused_hours: int,
    dry_run: bool,
) -> dict[str, int]:
    """Remove uploads never sent and older than ``unused_hours``: object, then row.

    **One upload at a time, under its row lock** (`SKIP LOCKED`): a send
    attaching it waits, then finds no row and is refused, so a message never
    carries a file whose object is gone. A failed object delete rolls back and
    leaves the row for the next run. A dry run counts and changes nothing.
    """
    abandoned = and_(_unsent(), MessageAttachment.created_at < unused_cutoff(now, unused_hours))
    if dry_run:
        count = (await session.execute(select(func.count()).where(abandoned))).scalar_one()
        await session.rollback()
        return message_sweep_counts(swept=count)

    candidates = (
        (
            await session.execute(
                select(MessageAttachment.id)
                .where(abandoned)
                .order_by(MessageAttachment.created_at)
                .limit(SWEEP_BATCH)
            )
        )
        .scalars()
        .all()
    )
    await session.rollback()
    swept = failed = 0
    for file_id in candidates:
        key = (
            await session.execute(
                select(MessageAttachment.storage_key)
                .where(MessageAttachment.id == file_id, abandoned)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if key is None:
            # Sent since it was listed, or being sent right now.
            await session.rollback()
            continue
        try:
            await asyncio.to_thread(storage.delete, key)
        except StorageError:
            await session.rollback()
            failed += 1
            continue
        await session.execute(delete(MessageAttachment).where(MessageAttachment.id == file_id))
        await session.commit()
        swept += 1
    if failed:
        logger.warning("message file sweep left objects for the next run", extra={"failed": failed})
    return message_sweep_counts(swept=swept, failed=failed)
