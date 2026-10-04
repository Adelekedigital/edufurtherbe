"""What a mentee may upload to answer an intake question, and how it is named.

**The type is decided from the bytes**, never from the filename or the client's
`Content-Type`: both are the uploader's claim. PDF is its magic number. A `.docx`
is a zip, so "starts with `PK`" would also admit any zip, a `.docm` with macros,
or an `.xlsx` — it is accepted only when `[Content_Types].xml` declares
**`/word/document.xml` itself** as a Word document's main part, the archive
holds that part, and it carries no VBA project anywhere. Declared, not merely
mentioned: an earlier substring test was passed by a `.docm` that planted a
second, decoy Word declaration on some other part.

**Macros are refused by their declared type, not only by their name.** Word
finds a VBA project through its content type, so a `vbaProject.bin` renamed
`m.dat` is still one; every part a package declares — by `Override` or by
`Default` extension — is checked, and any macro-enabled type or VBA project
refuses the file. The name check stays as a second layer. **An archive naming
one entry twice, in any case, is refused**: which copy a reader takes is the
reader's choice, so this check and Word could read different declarations.

**The declaration is parsed with `defusedxml`**, which refuses a DTD's entities
rather than expanding them, so the one XML document read here cannot be a
billion-laughs bomb. It is a pure parser — no I/O, no framework — which is why
it may sit in `domain/`.

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
import math
import unicodedata
import zipfile
import zlib
from urllib.parse import quote
from uuid import UUID
from xml.etree.ElementTree import ParseError

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring

from app.domain.enums import IntakeFileType
from app.domain.text import visible_only

PDF_MAGIC = b"%PDF-"
ZIP_MAGIC = b"PK\x03\x04"

#: A Word document's own main part. A macro-enabled `.docm`, a template, a
#: spreadsheet and a slide deck each declare a different one.
DOCX_MAIN = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
CONTENT_TYPES = "[Content_Types].xml"
DOCUMENT_PART = "word/document.xml"
#: `[Content_Types].xml`'s own namespace — an `Override` in any other is not one.
CONTENT_TYPES_NS = "{http://schemas.openxmlformats.org/package/2006/content-types}"
OVERRIDE = f"{CONTENT_TYPES_NS}Override"
DEFAULT = f"{CONTENT_TYPES_NS}Default"
#: Where Word keeps macros. A `.docx` never carries one, wherever it is put.
VBA_PROJECT = "vbaproject.bin"
#: A VBA project's declared type — how Word finds one, whatever it is called.
VBA_PROJECT_TYPE = "application/vnd.ms-office.vbaproject"
#: Every macro-enabled Office main part says so in its type.
MACRO_ENABLED = "macroenabled"

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

#: The window the per-person upload rate is counted over (#281). The *count*
#: is configuration (`INTAKE_UPLOADS_PER_HOUR`); the window is not, because the
#: setting's name promises an hour.
UPLOAD_RATE_WINDOW = dt.timedelta(hours=1)

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
            if len({name.lower() for name in names}) != len(entries):
                return False
            if CONTENT_TYPES not in names or DOCUMENT_PART not in names:
                return False
            if any(name.lower().rsplit("/", 1)[-1] == VBA_PROJECT for name in names):
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
    return len(declared) <= MAX_CONTENT_TYPES_BYTES and _declares_word_main(declared)


def _declares_word_main(content_types: bytes) -> bool:
    """Whether `/word/document.xml` is declared a Word document's main part.

    Part names compare without regard to case (ECMA-376 Part 2, 9.1.1.1.2).
    Any declaration of that part that is not Word's — two of them, one a
    `.docm`'s — refuses, rather than letting the first or last one win. So
    does any part of any name declared as a VBA project or macro-enabled.
    """
    try:
        root = fromstring(content_types)
    except ParseError, DefusedXmlException, ValueError:
        return False
    for declaration in (*root.iter(OVERRIDE), *root.iter(DEFAULT)):
        kind = (declaration.get("ContentType") or "").lower()
        if kind == VBA_PROJECT_TYPE or MACRO_ENABLED in kind:
            return False
    declared = {
        override.get("ContentType")
        for override in root.iter(OVERRIDE)
        if (override.get("PartName") or "").lower() == f"/{DOCUMENT_PART}"
    }
    return declared == {DOCX_MAIN}


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
    base = visible_only(base)
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


def upload_retry_after(oldest_in_window: dt.datetime, now: dt.datetime) -> int:
    """Whole seconds until the oldest upload in the window ages out of it.

    That is when the next upload is allowed, so it is what `Retry-After` says.
    Never below one: a header saying "retry in 0 seconds" invites a loop.
    """
    remaining = (oldest_in_window + UPLOAD_RATE_WINDOW - now).total_seconds()
    return max(1, math.ceil(remaining))
