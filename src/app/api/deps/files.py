"""Private files: the one byte check every upload passes, and the one download.

**Intake answers and message files are checked by the same function**
(decision 302; non-negotiable #8): the configured size, read one byte past so
no header is trusted, then the type from the bytes (`domain.intake_files`:
PDF or `.docx`, no macros, no renamed zip, no zip bomb). Each refusal is a
typed `422` at `/file`, so a client tells "too large" from "wrong type" by
`type` on either surface.

**Both live in one PRIVATE bucket**, `SUPABASE_INTAKE_BUCKET`, under keys made
of ids only (`{uploader}/{uuid}` for intake, `messages/{uploader}/{uuid}` for
a chat), and both are downloaded through this API, never by a signed link
(decision #77), so every read passes its reader check in the query.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated

import httpx
from fastapi import File, Request, UploadFile
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from app.api.deps.core import PRIVATE_NO_STORE, UPLOAD_TIMEOUT, _configured
from app.core.config import Settings, get_settings
from app.core.errors import (
    ConfigurationError,
    FileTooLargeError,
    FileTypeNotAllowedError,
    UpstreamError,
)
from app.domain.enums import IntakeFileType
from app.domain.intake_files import clean_filename, content_disposition, file_type
from app.infra.storage.supabase import StorageError, SupabaseStorage, intake_storage_for

__all__ = [
    "FILE_REFUSED",
    "MAX_FILE_MB",
    "CheckedFile",
    "UploadField",
    "checked_file",
    "private_storage",
    "streamed_file",
]

#: The default upload limit in MB, as the published spec quotes it.
MAX_FILE_MB = Settings.model_fields["intake_file_max_bytes"].default // (1024 * 1024)

#: What `checked_file`'s refusals publish, for every upload route's `422`.
FILE_REFUSED = (
    "`/problems/file-too-large`: over the size limit. "
    "`/problems/file-type-not-allowed`: empty, or not a PDF or Word document. "
    "`errors[0].pointer` is `/file`."
)

#: The multipart field every upload takes, described once.
UploadField = Annotated[
    UploadFile,
    File(description=f"A PDF or Word (.docx) file, up to {MAX_FILE_MB} MB by default."),
]


@lru_cache(maxsize=1)
def _process_storage() -> SupabaseStorage:
    storage = intake_storage_for(get_settings(), httpx.Client(timeout=UPLOAD_TIMEOUT))
    if storage is None:
        raise ConfigurationError("Supabase intake storage is not configured")
    return storage


def private_storage(request: Request) -> SupabaseStorage:
    """The private bucket, refused as misconfigured until it is named.

    The setting is checked before any injected client, so an app without the
    bucket configured refuses even where a test double is present.
    """
    if _configured(request).supabase_intake_bucket is None:
        raise ConfigurationError("SUPABASE_INTAKE_BUCKET is not set")
    injected: SupabaseStorage | None = getattr(request.app.state, "intake_storage", None)
    return injected or _process_storage()


@dataclass(frozen=True, slots=True)
class CheckedFile:
    """An upload that passed: its bytes, its type and the name to offer back."""

    payload: bytes
    kind: IntakeFileType
    filename: str


async def checked_file(file: UploadFile, settings: Settings) -> CheckedFile:
    """**The one byte check for every upload.** Size first, then type.

    The body limit in `api/limits.py` has already bounded the transfer; the
    limit here is the configured one on the *file*.
    """
    limit = settings.intake_file_max_bytes
    payload = await file.read(limit + 1)
    if len(payload) > limit:
        raise FileTooLargeError(
            f"that file is larger than {limit / (1024 * 1024):g} MB",
            field_errors=(("/file", "too large"),),
        )
    kind = file_type(payload)
    if kind is None:
        raise FileTypeNotAllowedError(
            "only PDF and Word (.docx) files are accepted",
            field_errors=(("/file", "not a PDF or Word document"),),
        )
    return CheckedFile(payload, kind, clean_filename(file.filename, kind))


async def streamed_file(
    request: Request, *, storage_key: str, filename: str, content_type: str
) -> StreamingResponse:
    """A private object as an attachment, streamed: the one download."""
    storage = private_storage(request)
    try:
        upstream = await asyncio.to_thread(storage.open_stream, storage_key)
    except StorageError as exc:
        raise UpstreamError("the file could not be read from storage") from exc
    return StreamingResponse(
        upstream.iter_bytes(),
        media_type=content_type,
        headers={
            "Content-Disposition": content_disposition(filename),
            # Never rendered inline and never re-guessed: a PDF is a document a
            # browser would otherwise open in this API's origin.
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": PRIVATE_NO_STORE,
        },
        background=BackgroundTask(upstream.close),
    )
