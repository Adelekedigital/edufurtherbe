"""Files in a chat: upload first, send with the id, download through the API.

**Upload, then send.** `POST /me/message-files` checks the file exactly as an
intake upload is checked (one function, `deps.files.checked_file`) and returns
a `file_id`; `POST /me/messages/{user_id}` sends it as `file_id`, attaching it
in the message's own transaction. Download goes through this API too, never a
signed link (decision #77), so every read passes the reader check in the query.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from fastapi.responses import StreamingResponse

from app.api.deps import MAX_FILE_MB, MessageFileDownloadDep, UploadedMessageFileDep
from app.api.limits import MAX_BODY_MB
from app.api.schemas.intake import IntakeFileRead
from app.core.config import Settings
from app.domain.enums import IntakeFileType
from app.domain.intake_files import MAX_PENDING_UPLOADS

router = APIRouter(prefix="/api/v1", tags=["messages"])

#: The defaults the published description quotes (the spec is generated from
#: defaults, which "unless this deployment configures otherwise" qualifies).
UNUSED_HOURS = Settings.model_fields["intake_file_unused_hours"].default
UPLOADS_PER_HOUR = Settings.model_fields["intake_uploads_per_hour"].default

UNAUTHENTICATED: dict[int | str, dict[str, str]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "The bearer token is absent, malformed, expired or wrongly signed."
    },
}


@router.post(
    "/me/message-files",
    status_code=status.HTTP_201_CREATED,
    response_model=IntakeFileRead,
    summary="Upload a file to send in a message",
    description=(
        "Send one file as `multipart/form-data` under the field name `file`. **PDF "
        "or Word (`.docx`) only, decided from the bytes**, with the same checks as "
        "an intake upload: the filename and the declared `Content-Type` are not "
        "consulted, and a macro-enabled document, a renamed zip or a zip bomb is "
        f"refused. Up to {MAX_FILE_MB} MB unless this deployment configures "
        "otherwise.\n\n"
        "Returns a `file_id` to send as `file_id` on `POST /me/messages/{user_id}`. "
        "The file is private: only you can download it until it is sent, then "
        "the two people in that chat.\n\n"
        f"**A file not sent within {UNUSED_HOURS} hours is deleted** unless this "
        f"deployment configures otherwise, and you may hold at most {MAX_PENDING_UPLOADS} "
        "unsent files at once. A sent file is kept.\n\n"
        f"**At most {UPLOADS_PER_HOUR} message files in any hour** unless this "
        "deployment configures otherwise, counted apart from intake uploads. The "
        "next is a `429` (`/problems/rate-limited`) whose `Retry-After` header is "
        "the number of seconds until another upload is allowed."
    ),
    responses={
        **UNAUTHENTICATED,
        status.HTTP_409_CONFLICT: {
            "description": f"You already hold {MAX_PENDING_UPLOADS} unsent files."
        },
        status.HTTP_413_CONTENT_TOO_LARGE: {
            "description": f"The request body exceeds {MAX_BODY_MB} MB."
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": (
                "`/problems/file-too-large`: over the size limit. "
                "`/problems/file-type-not-allowed`: empty, or not a PDF or Word "
                "document. `errors[0].pointer` is `/file`."
            )
        },
        status.HTTP_429_TOO_MANY_REQUESTS: {
            "description": (
                f"More than {UPLOADS_PER_HOUR} message files in the last hour. "
                "`Retry-After` says how many seconds until the next is allowed."
            ),
            "headers": {
                "Retry-After": {
                    "description": "Seconds until another upload is allowed.",
                    "schema": {"type": "integer", "minimum": 1},
                }
            },
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "description": "File uploads are not configured on this deployment."
        },
    },
)
async def upload_message_file(
    uploaded: UploadedMessageFileDep, response: Response
) -> IntakeFileRead:
    response.headers["Location"] = f"/api/v1/message-files/{uploaded['file_id']}"
    return IntakeFileRead(**uploaded)


@router.get(
    "/message-files/{file_id}",
    response_class=StreamingResponse,
    summary="Download a file from a chat",
    description=(
        "The file, streamed, as `Content-Disposition: attachment` with its "
        "original name (`filename*` carries it exactly, UTF-8). **Readable by "
        "you before you send it, then by the two people in the chat it was sent "
        "in.** Anyone else gets `404`, exactly as for an id that does not exist, "
        "and so does a file whose message was deleted or an upload that expired "
        "unsent."
    ),
    responses={
        **UNAUTHENTICATED,
        status.HTTP_200_OK: {
            "content": {kind.value: {} for kind in IntakeFileType},
            "description": "The file's bytes.",
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "No such file, or not yours to read. Indistinguishable."
        },
        status.HTTP_502_BAD_GATEWAY: {"description": "Storage could not be read."},
    },
)
async def download_message_file(download: MessageFileDownloadDep) -> StreamingResponse:
    return download
