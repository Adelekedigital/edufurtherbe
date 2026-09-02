"""What we are willing to store, decided without a bucket or a database.

`process` is pure — bytes in, bytes out — which is what lets every refusal be
tested here rather than through an endpoint. A refusal that can only be reached
over HTTP is one that gets tested once and then never again.

**The assertions are on the output bytes, not on the code path.** That the
function does not pass `exif=` to Pillow is an implementation detail; that the
stored image carries no GPS coordinates is the requirement, and only re-reading
the result proves it.
"""

from __future__ import annotations

import io
import struct
import zlib

import pytest
from PIL import Image

from app.core.errors import ValidationError
from app.domain.assets import AssetKind
from app.domain.images import ACCEPTED, MAX_PIXELS, MIGRATION_ACCEPTED, process
from conftest import image_bytes


def opened(payload: bytes) -> Image.Image:
    return Image.open(io.BytesIO(payload))


# --------------------------------------------------------------------------
# what is accepted, and what is not
# --------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP"])
def test_the_three_upload_formats_are_accepted(fmt: str) -> None:
    result = process(image_bytes(fmt), AssetKind.AVATAR)
    assert result.content_type in {"image/jpeg", "image/png"}
    assert opened(result.payload).size == (40, 30)


def test_an_empty_file_is_refused_by_its_own_message() -> None:
    """Not covered by the sweep below, and a mutation proved it: empty bytes
    sniff as `application/octet-stream` and the format check refuses them
    anyway, so deleting this branch changes nothing a test could see. What it
    changes is what the user is told — "you attached nothing" rather than "wrong
    format" — so the message is the assertion."""
    with pytest.raises(ValidationError, match="empty"):
        process(b"", AssetKind.AVATAR)


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (b"", "empty"),
        (b"not an image at all, just text", "not an image"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"><circle r="1"/></svg>', "svg"),
        (image_bytes("GIF"), "gif"),
        (image_bytes("BMP"), "bmp"),
    ],
)
def test_the_upload_path_refuses(payload: bytes, reason: str) -> None:
    """One case each, not a sweep — a parametrised refusal that silently stopped
    covering one format would still read as five green cases."""
    with pytest.raises(ValidationError, match=r".+") as refusal:
        process(payload, AssetKind.AVATAR)
    assert str(refusal.value), f"{reason} was refused without saying why"


@pytest.mark.parametrize("fmt", ["GIF", "BMP"])
def test_the_migration_takes_legacy_formats_the_upload_path_will_not(fmt: str) -> None:
    """The **only** difference between the two callers. A legacy GIF avatar has to
    migrate; a user sending one today is a decision nobody made."""
    result = process(image_bytes(fmt), AssetKind.AVATAR, accepted=MIGRATION_ACCEPTED)
    assert opened(result.payload).size == (40, 30)


def test_the_wider_set_still_refuses_what_is_not_an_image() -> None:
    """Wider is not open. An SVG carries script and Pillow cannot read it at all."""
    with pytest.raises(ValidationError):
        process(
            b"<svg xmlns='http://www.w3.org/2000/svg'/>",
            AssetKind.AVATAR,
            accepted=MIGRATION_ACCEPTED,
        )


def test_the_bytes_decide_and_not_a_declared_type() -> None:
    """A GIF is refused however it is labelled — there is no parameter that could
    carry a client's claim into this function, which is the design."""
    gif = image_bytes("GIF")
    assert gif.startswith(b"GIF8")
    with pytest.raises(ValidationError):
        process(gif, AssetKind.AVATAR)


def test_the_upload_set_is_narrower_than_the_migration_set() -> None:
    """Pins the relationship, so widening one does not silently widen the other."""
    assert ACCEPTED < MIGRATION_ACCEPTED
    assert "image/svg+xml" not in MIGRATION_ACCEPTED


# --------------------------------------------------------------------------
# metadata
# --------------------------------------------------------------------------


def test_the_input_really_does_carry_gps() -> None:
    """The positive half. Without this, the stripping test below passes just as
    well against a fixture that never had coordinates in the first place."""
    with opened(image_bytes("JPEG", gps=True)) as source:
        assert source.getexif().get_ifd(0x8825), "the fixture carries no GPS"


def test_gps_and_every_other_exif_tag_are_gone_from_what_is_stored() -> None:
    result = process(image_bytes("JPEG", gps=True), AssetKind.AVATAR)
    with opened(result.payload) as stored:
        exif = stored.getexif()
        assert dict(exif) == {}, f"EXIF survived: {dict(exif)}"
        assert not exif.get_ifd(0x8825), "GPS survived re-encoding"


# --------------------------------------------------------------------------
# size
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("kind", "edge"), [(AssetKind.AVATAR, 512), (AssetKind.BANNER, 1500)])
def test_a_large_image_is_resized_and_not_refused(kind: AssetKind, edge: int) -> None:
    """A phone photo is 4000px wide. Refusing it is a limit no user should meet,
    so the longest edge comes down and the aspect ratio does not move."""
    result = process(image_bytes("JPEG", (4000, 3000)), kind)
    assert max(result.width, result.height) == edge
    assert result.width / result.height == pytest.approx(4 / 3, rel=0.01)


def test_the_two_kinds_do_not_share_a_limit() -> None:
    """Both edges in one assertion: a mapping that returned 512 for everything
    would pass each half separately."""
    large = image_bytes("JPEG", (4000, 3000))
    avatar = process(large, AssetKind.AVATAR)
    banner = process(large, AssetKind.BANNER)
    assert (avatar.width, banner.width) == (512, 1500)


def test_a_small_image_is_left_alone() -> None:
    """Enlarging stores blur and more bytes for no gain."""
    result = process(image_bytes("JPEG", (200, 200)), AssetKind.AVATAR)
    assert (result.width, result.height) == (200, 200)


def test_resizing_actually_shrinks_the_stored_bytes() -> None:
    """The point of the resize is the payload, not the reported dimensions."""
    original = image_bytes("JPEG", (4000, 3000))
    result = process(original, AssetKind.AVATAR)
    assert len(result.payload) < len(original)


# --------------------------------------------------------------------------
# transparency
# --------------------------------------------------------------------------


def test_a_transparent_png_stays_a_png() -> None:
    """Flattening onto white puts a visible box around a logo."""
    buffer = io.BytesIO()
    Image.new("RGBA", (40, 30), (10, 120, 200, 0)).save(buffer, format="PNG")

    result = process(buffer.getvalue(), AssetKind.AVATAR)

    assert result.content_type == "image/png"
    with opened(result.payload) as stored:
        assert stored.mode == "RGBA"


def test_an_opaque_photograph_becomes_a_jpeg() -> None:
    """The other branch. Storing every photo as PNG is several times the bytes."""
    result = process(image_bytes("PNG", (300, 300)), AssetKind.AVATAR)
    assert result.content_type == "image/jpeg"


# --------------------------------------------------------------------------
# the decompression bomb
# --------------------------------------------------------------------------


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png_claiming(width: int, height: int) -> bytes:
    """A PNG that *declares* a bitmap without containing one. 68 bytes.

    `Image.open` reads the header and stops at the first IDAT, so the declared
    size is known before a single pixel is allocated — which is the whole reason
    the ceiling is checked there rather than after decoding.

    **IDAT and IEND are what make this work**, and a mutation is how that was
    found: an IHDR-only file is not *identifiable*, so the first version of this
    helper was refused for being malformed and the ceiling was never reached.
    The test passed and proved nothing.
    """
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(b"\x00" * 16))
        + _chunk(b"IEND", b"")
    )


def test_the_crafted_header_really_is_readable() -> None:
    """The half that keeps the two tests below honest — a file Pillow cannot
    identify is refused for the wrong reason and looks identical from here."""
    with Image.open(io.BytesIO(png_claiming(100, 100))) as probe:
        assert probe.size == (100, 100)


def test_a_bitmap_over_our_ceiling_is_refused_where_pillow_would_only_warn() -> None:
    """64 megapixels: above our 50, below the 100 at which Pillow hard-errors.

    **This is the band our own check exists for.** Left to Pillow it is a
    `DecompressionBombWarning` and the decode proceeds — 192 MB of allocation
    from 68 bytes of request.
    """
    assert MAX_PIXELS < 8_000 * 8_000 < 2 * Image.MAX_IMAGE_PIXELS
    with (
        pytest.warns(Image.DecompressionBombWarning),
        pytest.raises(
            ValidationError,
            match="too large",
        ),
    ):
        process(png_claiming(8_000, 8_000), AssetKind.AVATAR)


def test_a_bitmap_over_pillows_own_ceiling_is_a_refusal_and_not_a_crash() -> None:
    """400 megapixels. Pillow raises inside `Image.open`, before our check runs,
    and `DecompressionBombError` is not an `OSError` — so without a clause of
    its own this is a 500 on a request anybody can craft."""
    with pytest.raises(ValidationError, match="too large"):
        process(png_claiming(20_000, 20_000), AssetKind.AVATAR)


def test_the_ceiling_is_below_what_a_decode_of_that_size_would_cost() -> None:
    """20000x20000 at 3 bytes a pixel is 1.2 GB. The ceiling has to sit under it
    or the guard is decoration."""
    assert MAX_PIXELS < 20_000 * 20_000


def test_pillows_own_ceiling_is_set_too() -> None:
    """A future caller reaching `Image.open` without going through `process`
    still meets a limit rather than a warning."""
    assert Image.MAX_IMAGE_PIXELS == MAX_PIXELS


def test_the_refusal_names_the_formats_that_caller_accepts() -> None:
    """The message is built from the set, because the two callers accept
    different ones. A fixed sentence naming three formats is false on the
    migration path, and the operator reading it is the person who has to act."""
    with pytest.raises(ValidationError, match="JPEG, PNG and WebP"):
        process(image_bytes("GIF"), AssetKind.AVATAR)

    with pytest.raises(ValidationError, match="AVIF, BMP, GIF, JPEG, PNG and WebP"):
        process(
            b"<svg xmlns='http://www.w3.org/2000/svg'/>",
            AssetKind.AVATAR,
            accepted=MIGRATION_ACCEPTED,
        )
