"""Files a mentee uploads to answer an offering's `file_upload` questions.

**Upload first, answer with the id.** `POST /me/intake-files` takes one file and
returns a `file_id`; the booking's `answers[]` names it, and the booking links
it in its own transaction. Download goes through this API too — never a signed
link (decision #77) — so every read passes the reader check in the query.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from fastapi.responses import StreamingResponse

from app.api.deps import MAX_FILE_MB, IntakeFileDownloadDep, UploadedIntakeFileDep
from app.api.limits import MAX_BODY_MB
from app.api.schemas.intake import IntakeFileRead
from app.core.config import Settings
from app.domain.enums import IntakeFileType
from app.domain.intake_files import MAX_PENDING_UPLOADS

router = APIRouter(prefix="/api/v1", tags=["intake-files"])

#: The figures the published description quotes, read from where they are set.
#: The spec is generated from defaults, so these are the defaults — which is
#: what "unless this deployment configures otherwise" qualifies. The file limit
#: is `deps.MAX_FILE_MB`, which the upload's own field description also quotes.
UNUSED_HOURS = Settings.model_fields["intake_file_unused_hours"].default

UNAUTHENTICATED: dict[int | str, dict[str, str]] = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "The bearer token is absent, malformed, expired or wrongly signed."
    },
}


@router.post(
    "/me/intake-files",
    status_code=status.HTTP_201_CREATED,
    response_model=IntakeFileRead,
    summary="Upload a file to answer an intake question with",
    description=(
        "Send one file as `multipart/form-data` under the field name `file`. **PDF "
        "or Word (`.docx`) only, decided from the bytes** — the filename and the "
        "declared `Content-Type` are not consulted, and a macro-enabled `.docm` is "
        f"refused. Up to {MAX_FILE_MB} MB unless this deployment configures otherwise.\n\n"
        "Returns a `file_id` to send as `answers[].file_id` on `POST /sessions`. "
        "The file is private: only you, the mentor of the session it answers, and "
        "admins can download it.\n\n"
        f"**An upload no booking uses is deleted after {UNUSED_HOURS} hours** unless this "
        "deployment configures otherwise, and you may "
        f"hold at most {MAX_PENDING_UPLOADS} unused uploads at once. Files that "
        "answer a booking are kept for the deployment's retention period, which "
        "may be indefinitely."
    ),
    responses={
        **UNAUTHENTICATED,
        status.HTTP_409_CONFLICT: {
            "description": f"You already hold {MAX_PENDING_UPLOADS} unused uploads."
        },
        status.HTTP_413_CONTENT_TOO_LARGE: {
            "description": f"The request body exceeds {MAX_BODY_MB} MB."
        },
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "description": (
                "The file is empty, is not a PDF or Word document, or is over the "
                "size limit. `errors[0].pointer` is `/file`."
            )
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "description": "File uploads are not configured on this deployment."
        },
    },
)
async def upload_intake_file(uploaded: UploadedIntakeFileDep, response: Response) -> IntakeFileRead:
    response.headers["Location"] = f"/api/v1/intake-files/{uploaded['file_id']}"
    return IntakeFileRead(**uploaded)


@router.get(
    "/intake-files/{file_id}",
    response_class=StreamingResponse,
    summary="Download an intake file",
    description=(
        "The file, streamed, as `Content-Disposition: attachment` with its "
        "original name (`filename*` carries it exactly, UTF-8). **Readable by "
        "the person who uploaded it, the mentor of the session it answers, and "
        "admins.** Anyone else gets `404`, exactly as for an id that does not "
        "exist — and so does a file past its retention period."
    ),
    responses={
        **UNAUTHENTICATED,
        status.HTTP_200_OK: {
            "content": {kind.value: {} for kind in IntakeFileType},
            "description": "The file's bytes.",
        },
        status.HTTP_404_NOT_FOUND: {
            "description": "No such file, not yours to read, or deleted. Indistinguishable."
        },
        status.HTTP_502_BAD_GATEWAY: {"description": "Storage could not be read."},
    },
)
async def download_intake_file(download: IntakeFileDownloadDep) -> StreamingResponse:
    return download
