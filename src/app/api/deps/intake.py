"""Intake files: upload to the private bucket and the reader-checked download."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Request
from fastapi.responses import StreamingResponse

from app.api.deps.core import CurrentUserDep, SessionDep, _configured
from app.api.deps.files import UploadField, checked_file, private_storage, streamed_file
from app.core.errors import NotFoundError
from app.infra.db.intake_file_store import (
    delete_pending_upload,
    readable_file,
    store_intake_file,
)
from app.infra.db.session_answers_store import session_answers

# --------------------------------------------------------------------------
# Intake files
# --------------------------------------------------------------------------
#
# **In the private bucket** (`deps.files`), never `get_storage()`'s public one:
# a CV is personal data.


async def uploaded_intake_file(
    request: Request, user: CurrentUserDep, session: SessionDep, file: UploadField
) -> dict[str, Any]:
    """Check the file by its bytes (`checked_file`), store it privately, describe it."""
    storage = private_storage(request)
    settings = _configured(request)
    checked = await checked_file(file, settings)
    return await store_intake_file(
        session,
        storage,
        uploader_id=user["id"],
        filename=checked.filename,
        payload=checked.payload,
        kind=checked.kind,
        uploads_per_hour=settings.intake_uploads_per_hour,
    )


UploadedIntakeFileDep = Annotated[dict[str, Any], Depends(uploaded_intake_file)]


async def deleted_intake_file(file_id: UUID, user: CurrentUserDep, session: SessionDep) -> None:
    """The caller's unused upload, deleted and committed. Scoped to them in the write."""
    await delete_pending_upload(session, file_id=file_id, uploader_id=user["id"])
    await session.commit()


DeletedIntakeFileDep = Annotated[None, Depends(deleted_intake_file)]


async def intake_file_download(
    file_id: UUID, request: Request, user: CurrentUserDep, session: SessionDep
) -> StreamingResponse:
    """The file as an attachment, streamed, for its three readers only."""
    row = await readable_file(
        session, file_id=file_id, caller_id=user["id"], caller_is_admin=bool(user["is_admin"])
    )
    if row is None:
        raise NotFoundError("no such file")
    return await streamed_file(
        request,
        storage_key=row["storage_key"],
        filename=row["filename"],
        content_type=str(row["content_type"]),
    )


IntakeFileDownloadDep = Annotated[StreamingResponse, Depends(intake_file_download)]


async def session_intake_answers(
    session_id: UUID, user: CurrentUserDep, session: SessionDep
) -> list[dict[str, Any]]:
    """The booking's answers, for its mentee, its mentor or an admin; else 404."""
    rows = await session_answers(
        session, session_id, caller_id=user["id"], caller_is_admin=bool(user["is_admin"])
    )
    if rows is None:
        raise NotFoundError("no such session")
    return rows


SessionAnswersDep = Annotated[list[dict[str, Any]], Depends(session_intake_answers)]
