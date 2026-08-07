"""What to do about one profile image, decided without a network.

Three facts about the legacy assets shape everything here, and all three were
measured against the real export rather than assumed:

**1. The host allowlist runs the right way round.** 20 of 43 dev users have a
profile image, spread over three hosts — and *eleven* of them sit behind
``app.edufurther.org``, a custom domain that resolves to Bubble. A denylist like
``LIKE '%bubble%'`` would call those eleven already-migrated and skip them. So the
question asked here is **"is this already on storage we own"**, and everything
else needs re-hosting.

**2. File extensions lie.** A ``.gif`` and an ``.avif`` in the dev export both
return JFIF bytes. Naming the uploaded object from the source URL would publish
images with a ``Content-Type`` that disagrees with their content, which browsers
handle inconsistently and caches make permanent. The type is sniffed from the
bytes.

**3. Not every asset is retrievable.** One returns 401 with a zero-length body.
That is a reportable outcome, not a crash and not a reason to stop.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlparse
from uuid import UUID


class AssetKind(StrEnum):
    """Which image, and therefore which column it lands in."""

    AVATAR = "avatar"
    BANNER = "banner"


class AssetAction(StrEnum):
    """What a run will do about one image."""

    #: Already on our storage. Re-running must not fetch or upload it, which is
    #: what keeps a second pass free.
    SKIP = "skip"
    #: Hosted elsewhere; fetch it and put it somewhere we own.
    COPY = "copy"


# Magic bytes, in the order they must be tested. `imghdr` was removed in Python
# 3.13, and guessing from the extension is what fact 2 above rules out.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)

EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/avif": "avif",
    "image/bmp": "bmp",
    "image/svg+xml": "svg",
}

#: What an object is served as when the bytes match nothing known. Deliberately
#: not `image/*` — mislabelling an unknown blob as an image is how a non-image
#: ends up rendered in a profile page.
FALLBACK_CONTENT_TYPE = "application/octet-stream"


def sniff_content_type(payload: bytes) -> str:
    """The real media type of these bytes, ignoring whatever the URL claimed.

    RIFF/WEBP and the ISO-BMFF family (AVIF, HEIC) carry their brand at an
    offset rather than at byte zero, so they are checked separately. SVG is text
    and has no magic number at all — it is recognised by its root element, after
    skipping any XML declaration or byte-order mark.
    """
    if not payload:
        return FALLBACK_CONTENT_TYPE

    for signature, content_type in _SIGNATURES:
        if payload.startswith(signature):
            return content_type

    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"

    # ISO base media format: a 4-byte length, then `ftyp`, then the brand.
    if payload[4:8] == b"ftyp":
        brand = payload[8:12]
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand in (b"heic", b"heix", b"heim", b"heis"):
            return "image/heic"

    head = payload[:512].lstrip(b"\xef\xbb\xbf").lstrip()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in payload[:2048]):
        return "image/svg+xml"

    return FALLBACK_CONTENT_TYPE


def extension_for(content_type: str) -> str:
    """The extension to store the object under. Never taken from the source URL."""
    return EXTENSIONS.get(content_type, "bin")


def is_ours(url: str, storage_host: str) -> bool:
    """Is this URL already served from storage we control?

    **An allowlist, and that is the whole point.** The obvious test —
    "does it mention bubble" — reports eleven of the sixteen Bubble-hosted dev
    assets as already migrated, because they are served from the custom domain
    ``app.edufurther.org``. Asking whether a URL is *ours* has no such gap: a host
    we do not recognise is one we do not control, whatever it is called.
    """
    if not url:
        return False
    parsed = urlparse(url if "//" in url else f"https://{url}")
    return bool(parsed.netloc) and parsed.netloc.lower() == storage_host.lower()


def decide(url: str | None, storage_host: str) -> AssetAction | None:
    """What to do about one image. ``None`` when there is no image at all."""
    if not url or not url.strip():
        return None
    return AssetAction.SKIP if is_ours(url, storage_host) else AssetAction.COPY


def digest(payload: bytes) -> str:
    """A short content hash, used as the object's filename.

    Three jobs at once: it makes a re-run idempotent without asking storage what
    it holds, it makes the object path unguessable despite the bucket being
    public, and it means a changed image gets a new URL rather than fighting a
    CDN cache.
    """
    return hashlib.sha256(payload).hexdigest()[:32]


def object_path(user_id: UUID, kind: AssetKind, payload: bytes, content_type: str) -> str:
    """Where this image lives in the bucket.

    Keyed on **our** user id, never Bubble's — the identifier rule this project
    keeps: translate at the boundary, and do not let a vendor's identifier become
    a path other systems depend on.
    """
    return f"users/{user_id}/{kind}-{digest(payload)}.{extension_for(content_type)}"


@dataclass(frozen=True, slots=True)
class AssetReport:
    """What a run did, for the operator watching it.

    Deliberately **not** ``provisioning.Outcome``. That type counts
    created/linked/skipped, and `linked` has no meaning here; forcing two
    different result shapes into one name would be a worse defect than having two
    small dataclasses. Non-negotiable #8 is about one representation of one
    *rule*, not about collapsing distinct vocabularies.
    """

    copied: int = 0
    skipped: int = 0
    absent: int = 0
    failed: tuple[str, ...] = ()

    def summary(self) -> str:
        """The counts, for stdout. Every asset lands in exactly one of these."""
        return "\n".join(
            [
                f"copied  {self.copied}",
                f"skipped {self.skipped} (already ours)",
                f"absent  {self.absent} (no image, or the source would not serve it)",
                f"failed  {len(self.failed)}",
            ]
        )

    def failures(self) -> list[str]:
        """One line per failure, for stderr — matching the other migration CLIs."""
        return [f"FAILED {failure}" for failure in self.failed]
