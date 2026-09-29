"""What a mentee may upload to answer an intake question, and how it is named.

**The type is decided from the bytes**, never from the filename or the client's
`Content-Type`: both are the uploader's claim. PDF is its magic number. A `.docx`
is a zip, so "starts with `PK`" would also admit any zip, a `.docm` with macros,
or an `.xlsx` — it is accepted only when the archive declares a Word document's
main part in `[Content_Types].xml` and holds `word/document.xml`.

**Nothing in the archive is decompressed except that one small part**, and only
up to a bound. A docx is still a zip that a mentor's Word will open, so a zip
bomb is refused by what its directory declares: too many entries, or too many
bytes once inflated. The declared sizes can lie; the one read here is capped
regardless, and the rest is never inflated on this side.

**The storage key is built from ids only.** The uploader's filename never
reaches a path, so there is no traversal to sanitise away; it is kept only to
offer back on download, cleaned on the way in and encoded on the way out.
"""

from __future__ import annotations

import datetime as dt
import io
import unicodedata
import zipfile
import zlib
from urllib.parse import quote
from uuid import UUID

from app.domain.enums import IntakeFileType

PDF_MAGIC = b"%PDF-"
ZIP_MAGIC = b"PK\x03\x04"

#: A Word document's own main part. A macro-enabled `.docm`, a template, a
#: spreadsheet and a slide deck each declare a different one.
DOCX_MAIN = b"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
CONTENT_TYPES = "[Content_Types].xml"
DOCUMENT_PART = "word/document.xml"

#: A real CV is a few dozen entries; a few hundred with embedded images.
MAX_DOCX_ENTRIES = 1000
#: Declared bytes once inflated. A 5 MB file of XML inflates roughly tenfold.
MAX_DOCX_INFLATED = 64 * 1024 * 1024
#: `[Content_Types].xml` is a few KB; read no more than this of it.
MAX_CONTENT_TYPES_BYTES = 256 * 1024

#: What a filename is cut to, the column's own bound.
MAX_FILENAME_LENGTH = 255

#: Uploads one person may hold that answer no booking yet. Enough for every
#: file question on a few forms at once; an upload is free, so without a bound
#: one account could fill the bucket.
MAX_PENDING_UPLOADS = 10

EXTENSION = {IntakeFileType.PDF: ".pdf", IntakeFileType.DOCX: ".docx"}


def file_type(payload: bytes) -> IntakeFileType | None:
    """The accepted type these bytes are, or ``None`` if they are neither."""
    if payload.startswith(PDF_MAGIC):
        return IntakeFileType.PDF
    if payload.startswith(ZIP_MAGIC) and _is_docx(payload):
        return IntakeFileType.DOCX
    return None


def _is_docx(payload: bytes) -> bool:
    # Every way a hostile archive can fail to parse is a refusal, not a 500.
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_DOCX_ENTRIES:
                return False
            if sum(entry.file_size for entry in entries) > MAX_DOCX_INFLATED:
                return False
            names = {entry.filename for entry in entries}
            if CONTENT_TYPES not in names or DOCUMENT_PART not in names:
                return False
            with archive.open(CONTENT_TYPES) as part:
                declared = part.read(MAX_CONTENT_TYPES_BYTES + 1)
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        zlib.error,
        ValueError,
        NotImplementedError,
        RuntimeError,
        EOFError,
        OSError,
        KeyError,
    ):
        return False
    return len(declared) <= MAX_CONTENT_TYPES_BYTES and DOCX_MAIN in declared


def clean_filename(name: str | None, kind: IntakeFileType) -> str:
    """The uploader's filename, safe to store and to offer back.

    The last path segment only, without control or format characters, without
    leading dots, cut to the column's bound — and ending in the extension of
    the type the bytes actually are, so a PDF called `cv.exe` downloads as
    `cv.exe.pdf` and opens as what it is.
    """
    extension = EXTENSION[kind]
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = unicodedata.normalize("NFC", base)
    base = "".join(ch for ch in base if not unicodedata.category(ch).startswith("C"))
    base = base.strip()
    if base.lower().endswith(extension):
        base = base[: -len(extension)]
    base = base.lstrip(".").strip()
    base = base[: MAX_FILENAME_LENGTH - len(extension)] or "file"
    return f"{base}{extension}"


def content_disposition(filename: str) -> str:
    """A download header naming ``filename``, and nothing else it could say.

    RFC 6266: an ASCII `filename` for old clients, with every character that
    could end the quoted string or the header replaced, and the exact name as
    RFC 5987 `filename*`, percent-encoded so no byte of it is structural.
    """
    fallback = "".join(ch if " " <= ch <= "~" and ch not in '"\\;%' else "_" for ch in filename)
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


def storage_key(uploader_id: UUID, file_id: UUID) -> str:
    """Where the object lives in the private bucket: ids, and only ids."""
    return f"{uploader_id}/{file_id}"


def retention_cutoff(now: dt.datetime, days: int | None) -> dt.datetime | None:
    """Files uploaded before this instant are past retention; ``None`` keeps all."""
    return None if days is None else now - dt.timedelta(days=days)


def unused_cutoff(now: dt.datetime, hours: int) -> dt.datetime:
    """Uploads still unlinked and older than this are abandoned."""
    return now - dt.timedelta(hours=hours)
