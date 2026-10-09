"""Intake files: upload to the private bucket and the reader-checked download."""

from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Annotated, Any
from uuid import UUID

import httpx
from fastapi import Depends, File, Request, UploadFile
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from app.api.deps.core import UPLOAD_TIMEOUT, CurrentUserDep, SessionDep, _configured
from app.core.config import Settings, get_settings
from app.core.errors import (
    ConfigurationError,
    NotFoundError,
    UpstreamError,
    ValidationError,
)
from app.domain.intake_files import clean_filename, content_disposition, file_type
from app.infra.db.intake_file_store import readable_file, store_intake_file
from app.infra.db.session_answers_store import session_answers

# `get_session` is aliased: this module already has one, and it is the **database
# session** dependency at line 142. Two callables with that name in one file is a
# collision a reader resolves by scrolling, and the wrong one is a plausible
# mistake rather than an obvious error — `bubble_id` shadowed a local the same
# way in the M4 transform and raised `UnboundLocalError` far from the edit.
from app.infra.storage.supabase import StorageError, SupabaseStorage, intake_storage_for

# --------------------------------------------------------------------------
# Intake files
# --------------------------------------------------------------------------
#
# **A private bucket of its own**, never `get_storage()`'s public one: a CV is
# personal data. Uploaded to and downloaded through this API, never by a signed
# link (decision #77), so every read passes the reader check in the query.


@lru_cache(maxsize=1)
def _process_intake_storage() -> SupabaseStorage:
    storage = intake_storage_for(get_settings(), httpx.Client(timeout=UPLOAD_TIMEOUT))
    if storage is None:
        raise ConfigurationError("Supabase intake storage is not configured")
    return storage


#: The default upload limit in MB, as the published spec quotes it — the upload's
#: field description here and the route's description in `routes/intake_files`.
MAX_FILE_MB = Settings.model_fields["intake_file_max_bytes"].default // (1024 * 1024)


def intake_storage(request: Request) -> SupabaseStorage:
    """The private intake bucket — refused as misconfigured until it is named.

    The setting is checked before any injected client, so an app without the
    bucket configured refuses even where a test double is present.
    """
    if _configured(request).supabase_intake_bucket is None:
        raise ConfigurationError("SUPABASE_INTAKE_BUCKET is not set")
    injected: SupabaseStorage | None = getattr(request.app.state, "intake_storage", None)
    return injected or _process_intake_storage()


async def uploaded_intake_file(
    request: Request,
    user: CurrentUserDep,
    session: SessionDep,
    file: Annotated[
        UploadFile,
        File(description=f"A PDF or Word (.docx) file, up to {MAX_FILE_MB} MB by default."),
    ],
) -> dict[str, Any]:
    """Check the file by its bytes, store it privately, describe it.

    The body limit in `api/limits.py` has already bounded the transfer; the
    limit here is the configured one on the *file*, read one byte past so no
    header is trusted.
    """
    storage = intake_storage(request)
    settings = _configured(request)
    limit = settings.intake_file_max_bytes
    payload = await file.read(limit + 1)
    if len(payload) > limit:
        raise ValidationError(
            f"that file is larger than {limit / (1024 * 1024):g} MB",
            field_errors=(("/file", "too large"),),
        )
    kind = file_type(payload)
    if kind is None:
        raise ValidationError(
            "only PDF and Word (.docx) files are accepted",
            field_errors=(("/file", "not a PDF or Word document"),),
        )
    return await store_intake_file(
        session,
        storage,
        uploader_id=user["id"],
        filename=clean_filename(file.filename, kind),
        payload=payload,
        kind=kind,
        uploads_per_hour=settings.intake_uploads_per_hour,
    )


UploadedIntakeFileDep = Annotated[dict[str, Any], Depends(uploaded_intake_file)]


async def intake_file_download(
    file_id: UUID, request: Request, user: CurrentUserDep, session: SessionDep
) -> StreamingResponse:
    """The file as an attachment, streamed, for its three readers only."""
    row = await readable_file(
        session, file_id=file_id, caller_id=user["id"], caller_is_admin=bool(user["is_admin"])
    )
    if row is None:
        raise NotFoundError("no such file")
    storage = intake_storage(request)
    try:
        upstream = await asyncio.to_thread(storage.open_stream, row["storage_key"])
    except StorageError as exc:
        raise UpstreamError("the file could not be read from storage") from exc
    return StreamingResponse(
        upstream.iter_bytes(),
        media_type=str(row["content_type"]),
        headers={
            "Content-Disposition": content_disposition(row["filename"]),
            # Never rendered inline and never re-guessed: a PDF is a document a
            # browser would otherwise open in this API's origin.
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
        background=BackgroundTask(upstream.close),
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
