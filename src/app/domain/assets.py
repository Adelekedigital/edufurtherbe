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
    #: Hosted somewhere we do not recognise. Not fetched, and reported by host so
    #: a legitimate source can be added deliberately rather than discovered by a
    #: request leaving the network.
    REFUSE = "refuse"


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


# Hosts this migration will fetch from. Exact matches, plus suffixes for the two
# providers that vary the subdomain.
#
# **Every legacy image has one of two origins**, and both are closed sets: a file
# uploaded into Bubble, or an avatar an OAuth provider gave us at sign-up. Nobody
# could type a URL, so an address outside this list is not a source we have — it
# is a value nobody expected, and fetching it is how a migration reaches an
# address the person running it never chose.
FETCHABLE_HOSTS: frozenset[str] = frozenset({"app.edufurther.org"})

#: Matched on a dot boundary, so ``evil-cdn.bubble.io.attacker.test`` does not pass.
FETCHABLE_SUFFIXES: tuple[str, ...] = (
    ".cdn.bubble.io",  # Bubble uploads; the subdomain is a per-application hash
    ".googleusercontent.com",  # Google avatars — lh3 through lh6
    ".licdn.com",  # LinkedIn avatars
)


def is_fetchable(url: str) -> bool:
    """May this migration make a request to this address?

    Separate from ``is_ours`` on purpose: that one asks "is this already
    migrated", and answering it does not license a network call. An allowlist is
    the only form that works here — a denylist of internal ranges is defeated by
    a redirect, by DNS, and by every address nobody thought of.
    """
    if not url:
        return False
    parsed = urlparse(url if "//" in url else f"https://{url}")
    if parsed.scheme not in {"http", "https"}:
        return False
    host = parsed.hostname  # `hostname` strips port and credentials; `netloc` does not
    if not host:
        return False
    host = host.lower()
    return host in FETCHABLE_HOSTS or host.endswith(FETCHABLE_SUFFIXES)


def host_of(url: str | None) -> str:
    """The host, for reporting a refusal without echoing the whole address.

    A URL from an unexpected source may carry a path or query nobody should paste
    into a terminal or a ticket. The host is the part a decision is made about.
    """
    if not url:
        return "<no host>"
    return (urlparse(url if "//" in url else f"https://{url}").hostname or "<no host>").lower()


def decide(url: str | None, storage_host: str) -> AssetAction | None:
    """What to do about one image. ``None`` when there is no image at all."""
    if not url or not url.strip():
        return None
    if is_ours(url, storage_host):
        return AssetAction.SKIP
    return AssetAction.COPY if is_fetchable(url) else AssetAction.REFUSE


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
    #: Hosts the run declined to fetch from, deduplicated. Hosts rather than
    #: URLs: the next action is to decide whether a *source* is legitimate,
    #: and one line per user would bury that under repetition.
    refused_hosts: tuple[str, ...] = ()

    def summary(self) -> str:
        """The counts, for stdout. Every asset lands in exactly one of these."""
        return "\n".join(
            [
                f"copied  {self.copied}",
                f"skipped {self.skipped} (already ours)",
                f"absent  {self.absent} (no image, or the source would not serve it)",
                f"failed  {len(self.failed)}",
                f"refused {len(self.refused_hosts)} unrecognised host(s)",
            ]
        )

    def failures(self) -> list[str]:
        """One line per failure, for stderr — matching the other migration CLIs."""
        lines = [f"FAILED {failure}" for failure in self.failed]
        # Refusals are not failures: nothing broke, and the run is still correct.
        # They are on the same stream because both need somebody to look.
        lines += [f"REFUSED unrecognised host {host}" for host in self.refused_hosts]
        return lines
