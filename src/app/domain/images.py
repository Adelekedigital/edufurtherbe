"""Turning whatever a user uploaded into what we are willing to store.

**This is the only module in the project that decodes an untrusted file
format**, which is a narrower claim than it first sounds and is the one that
holds. Hostile bytes reach compiled code all over this service already: uvicorn
parses every HTTP request with httptools in C, pydantic-core parses every JSON
body in Rust, and cryptography verifies every token signature through OpenSSL.

The difference is what they are parsing. A protocol parser handles a grammar we
constrain, and is exercised by essentially every deployment on earth. A **media
decoder** handles dozens of container and codec formats built from length
fields, offsets and compression — historically the richest memory-safety surface
in any dependency set, and the reason the order of operations here matters more
than usual:

    sniff the format  ->  read the header  ->  refuse or allocate

Nothing is decoded until the header says how large the bitmap would be. A 4 KB
PNG can declare 20,000 x 20,000 pixels, and decoding it first to find out is the
denial of service.

WHY RE-ENCODE EVERY IMAGE
=========================
Because it is the only way stripping is guaranteed. Copying bytes preserves EXIF
— including the GPS coordinates a phone writes into a photo — and ADR 0019 left
that open, noting the migrated originals carry it and that it "is worth deciding
before profiles become public in M2".

Re-encoding also makes the answer uniform: there is no population of stripped and
unstripped images to reason about later, because every image entering storage
goes through here, from the upload endpoint and from the asset migration alike.

DOWNSCALE, NEVER UPSCALE
========================
A phone photo arrives at 4000px and is stored at 512. A 200px avatar stays 200px
— enlarging it stores blur and a larger file for no gain. Consumer platforms
resize rather than refuse, and refusing somebody's photo for having too many
pixels is a limit no user should ever meet.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

from app.core.errors import ValidationError
from app.domain.assets import AssetKind, sniff_content_type

#: What the **upload** path accepts, which is narrower than what the migration
#: reads. `sniff_content_type` also knows GIF and BMP because the Bubble export
#: contains them; taking them from a user is a different decision, and an
#: animated GIF as an avatar is a feature nobody asked for.
ACCEPTED = frozenset({"image/jpeg", "image/png", "image/webp"})

#: What the **migration** accepts, which is wider by exactly the formats the
#: legacy export contains and a user may no longer send: GIF, BMP and AVIF.
#:
#: Wider rather than absent, because "anything the decoder will take" is not a
#: set anybody chose — Pillow reads ICO, TIFF and PSD too. SVG stays out on both
#: paths: Pillow cannot decode it at all, and an SVG is a document that can carry
#: script, so a refusal here is the right outcome rather than a gap.
#:
#: Verified against Pillow 12.3 in this project: all six decode, and GIF arrives
#: in palette mode, so a transparent one is re-encoded to PNG rather than
#: flattened onto white.
MIGRATION_ACCEPTED = ACCEPTED | frozenset({"image/gif", "image/bmp", "image/avif"})

#: The largest image file we will accept, before anything is decoded.
#:
#: Profile-image limits across comparable products sit in the 1-8 MB band; 5 MB
#: is comfortably inside it and far more than a 512px avatar needs. **This is the
#: only limit an ordinary user can meet.** It bounds the *file*; the ceiling on
#: the whole request body lives in `api/limits.py`, because by the time this
#: module sees anything the body has already been read.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024

#: The largest bitmap we will allocate, in pixels.
#:
#: **A denial-of-service guard, not a limit a user meets.** 50 megapixels is
#: about 8500 x 6000 — larger than any phone or camera in normal use, and far
#: below the ~1.2 GB a 20,000 x 20,000 decode would want. It is reached from the
#: header, so the bytes on the wire say nothing about the memory demanded: 68 of
#: them can declare 400 megapixels.
#:
#: Pillow has its own version of this and the two are not interchangeable. Its
#: default *warns* and decodes anyway below twice `MAX_IMAGE_PIXELS`, and
#: hard-errors above — with a `DecompressionBombError` that is **not** an
#: `OSError`, raised inside `Image.open` before the check below runs. `process`
#: catches it explicitly for that reason.
MAX_PIXELS = 50_000_000

# Pillow's own ceiling, set to ours. Left at its default it emits a
# `DecompressionBombWarning` and decodes anyway — a warning nobody reads is not a
# control. This makes the library refuse even if a future caller reaches
# `Image.open` without going through `process`.
Image.MAX_IMAGE_PIXELS = MAX_PIXELS

#: The longest edge we store, per kind. Anything larger is downscaled.
MAX_EDGE: dict[AssetKind, int] = {
    AssetKind.AVATAR: 512,
    AssetKind.BANNER: 1500,
}

#: What we re-encode to. JPEG for photographs, PNG where transparency matters —
#: decided from the source, because flattening a transparent PNG onto white
#: produces a visible box around a logo.
_TRANSPARENT_MODES = frozenset({"RGBA", "LA", "P"})


@dataclass(frozen=True, slots=True)
class ProcessedImage:
    """What we will store, and what it is."""

    payload: bytes
    content_type: str
    width: int
    height: int


#: How a media type is written in a message to a person. Only the types either
#: caller accepts — anything else never reaches the sentence.
_DISPLAY_NAMES = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WebP",
    "image/gif": "GIF",
    "image/bmp": "BMP",
    "image/avif": "AVIF",
}


def _named(accepted: frozenset[str]) -> str:
    """ "JPEG, PNG and WebP" — the accepted set, as a sentence."""
    names = sorted(_DISPLAY_NAMES.get(one, one) for one in accepted)
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _refuse(detail: str) -> ValidationError:
    """A caller error, and phrased for the caller.

    No library exception text: Pillow's messages name file offsets and internal
    modes, which tell an uploader nothing and tell a prober something.
    """
    return ValidationError(detail)


def process(
    payload: bytes, kind: AssetKind, *, accepted: frozenset[str] = ACCEPTED
) -> ProcessedImage:
    """Validate, downscale and re-encode. Raises ``ValidationError`` if refused.

    Pure: bytes in, bytes out, no network and no storage — so every refusal
    above can be tested without a bucket, which is what keeps them tested.

    ``accepted`` is the **only** difference between the two callers. Everything
    that matters — the pixel ceiling, the downscale, the metadata being dropped —
    is identical for an upload and for a migrated image, because a rule that
    holds on one entry point and not the other is the rule this project has been
    burned by. Pass `MIGRATION_ACCEPTED` for the legacy formats.
    """
    if not payload:
        raise _refuse("the uploaded file is empty")

    content_type = sniff_content_type(payload)
    if content_type not in accepted:
        # The **bytes** decide, never the declared `Content-Type`. A client can
        # say anything; a JPEG magic number is a fact.
        #
        # The message is built from `accepted` rather than written out, because
        # the two callers accept different sets — a fixed sentence naming three
        # formats tells the operator running the migration something false.
        raise _refuse(f"only {_named(accepted)} images are accepted")

    try:
        # `open` reads the header only. The size is known here, before any
        # bitmap exists, which is the whole point of checking now.
        with Image.open(io.BytesIO(payload)) as probe:
            width, height = probe.size
            if width * height > MAX_PIXELS:
                raise _refuse("that image is too large to process")
            if width == 0 or height == 0:
                raise _refuse("that image has no dimensions")

            transparent = probe.mode in _TRANSPARENT_MODES or "transparency" in probe.info
            image = probe.convert("RGBA" if transparent else "RGB")
    except Image.DecompressionBombError as exc:
        # **Raised inside `Image.open`, so it arrives before the check above** —
        # Pillow hard-errors at twice `MAX_IMAGE_PIXELS` and only warns below
        # that, which is why both exist. It does not derive from `OSError`, so
        # without this clause a crafted header is a 500 rather than a refusal.
        raise _refuse("that image is too large to process") from exc
    except UnidentifiedImageError as exc:
        raise _refuse("that file is not an image we can read") from exc
    except OSError as exc:
        # A truncated or malformed file. Pillow raises `OSError` for both, and
        # neither is a server fault.
        raise _refuse("that image could not be read") from exc

    limit = MAX_EDGE[kind]
    if max(image.size) > limit:
        # `thumbnail` preserves the aspect ratio and **never enlarges**, which is
        # the behaviour we want on both counts.
        image.thumbnail((limit, limit), Image.Resampling.LANCZOS)

    buffer = io.BytesIO()
    if transparent:
        image.save(buffer, format="PNG", optimize=True)
        stored_type = "image/png"
    else:
        # `quality=85` is the usual photographic default: visually lossless at
        # this size, and a fraction of the bytes of 95.
        image.save(buffer, format="JPEG", quality=85, optimize=True)
        stored_type = "image/jpeg"

    # **No `exif=` argument, deliberately.** Pillow writes only what it is given,
    # so omitting it is what drops the camera metadata — there is no "strip"
    # call to forget, and a test asserts the GPS tags are gone rather than
    # trusting that.
    return ProcessedImage(
        payload=buffer.getvalue(),
        content_type=stored_type,
        width=image.width,
        height=image.height,
    )
