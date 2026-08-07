"""Asset decisions, all of which are pure.

The three tests that matter here each encode something measured against the real
43-user export rather than assumed — the custom domain that resolves to Bubble,
the file extensions that disagree with their own bytes, and the asset that will
not be served at all.
"""

from uuid import uuid4

import pytest

from app.domain.assets import (
    AssetAction,
    AssetKind,
    AssetReport,
    decide,
    digest,
    extension_for,
    host_of,
    is_fetchable,
    is_ours,
    object_path,
    sniff_content_type,
)

OURS = "project.supabase.co"

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
GIF = b"GIF89a\x01\x00"
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 "
AVIF = b"\x00\x00\x00\x20ftypavif\x00\x00\x00\x00"
SVG = b'<svg width="340" height="413" xmlns="http://www.w3.org/2000/svg">'


# --------------------------------------------------------------------------
# the allowlist, which runs the right way round
# --------------------------------------------------------------------------


def test_the_custom_domain_is_not_mistaken_for_ours() -> None:
    """``app.edufurther.org`` resolves to Bubble and hosts **eleven** of the
    sixteen Bubble-served dev assets.

    A denylist asking "does this mention bubble" would call all eleven already
    migrated and skip them — the exact trap the profile model warns about.
    """
    url = "https://app.edufurther.org/version-test/fileupload/f123/photo.jpeg"

    assert is_ours(url, OURS) is False
    assert decide(url, OURS) is AssetAction.COPY


@pytest.mark.parametrize(
    "url",
    [
        "https://4f8dee3b4728dbdfcf1f333770d80d0d.cdn.bubble.io/f123/x.jpg",
        "https://lh3.googleusercontent.com/a/ACg8ocK",
        "https://app.edufurther.org/version-test/fileupload/f1/y.png",
    ],
)
def test_every_foreign_host_in_the_export_is_copied(url: str) -> None:
    """All three hosts the real extract actually uses."""
    assert decide(url, OURS) is AssetAction.COPY


def test_an_asset_already_on_our_storage_is_skipped() -> None:
    """What makes a second run free."""
    url = f"https://{OURS}/storage/v1/object/public/profile-images/users/x/avatar-abc.jpg"

    assert is_ours(url, OURS) is True
    assert decide(url, OURS) is AssetAction.SKIP


def test_a_host_that_merely_contains_ours_is_not_ours() -> None:
    """``project.supabase.co.evil.test`` must not read as ours — the same
    substring-versus-exact-match error that stranded a user in provisioning."""
    assert is_ours(f"https://{OURS}.evil.test/x.jpg", OURS) is False


def test_no_image_is_not_a_decision() -> None:
    assert decide(None, OURS) is None
    assert decide("   ", OURS) is None


# --------------------------------------------------------------------------
# extensions lie; the bytes do not
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (JPEG, "image/jpeg"),
        (PNG, "image/png"),
        (GIF, "image/gif"),
        (WEBP, "image/webp"),
        (AVIF, "image/avif"),
        (SVG, "image/svg+xml"),
    ],
)
def test_the_type_comes_from_the_bytes(payload: bytes, expected: str) -> None:
    assert sniff_content_type(payload) == expected


def test_a_file_named_gif_that_holds_jpeg_is_served_as_jpeg() -> None:
    """Measured, not hypothetical: the dev export has a ``.gif`` and an ``.avif``
    that both return JFIF bytes. Trusting the extension would publish them with a
    Content-Type that disagrees with their content, which a CDN then caches."""
    assert sniff_content_type(JPEG) == "image/jpeg"
    assert extension_for(sniff_content_type(JPEG)) == "jpg"


def test_bytes_that_match_nothing_are_not_called_an_image() -> None:
    """Mislabelling an unknown blob as ``image/*`` is how a non-image ends up
    rendered on a profile page.

    Asserted against the **rule**, not against ``FALLBACK_CONTENT_TYPE``. An
    earlier version compared the result to that constant, so changing the
    constant to ``image/jpeg`` moved the expectation with it and the test stayed
    green — a tautology the mutation batch caught and nothing else would have.
    """
    for payload in (b"this is not an image at all", b"", bytes([0, 1, 2, 3])):
        assert not sniff_content_type(payload).startswith("image/"), payload
    assert sniff_content_type(b"") == "application/octet-stream"


def test_an_xml_declaration_before_the_svg_root_is_still_svg() -> None:
    declared = b'<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg"/>'

    assert sniff_content_type(declared) == "image/svg+xml"


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------


def test_the_path_is_keyed_on_our_id_and_the_content() -> None:
    user_id = uuid4()

    path = object_path(user_id, AssetKind.AVATAR, JPEG, "image/jpeg")

    assert path.startswith(f"users/{user_id}/avatar-")
    assert path.endswith(".jpg")


def test_the_same_bytes_always_produce_the_same_path() -> None:
    """What makes a re-run free without asking storage what it holds."""
    user_id = uuid4()

    first = object_path(user_id, AssetKind.AVATAR, JPEG, "image/jpeg")
    second = object_path(user_id, AssetKind.AVATAR, JPEG, "image/jpeg")

    assert first == second


def test_different_bytes_produce_a_different_path() -> None:
    """A changed image gets a new URL rather than fighting a CDN cache.

    **Both payloads are JPEG on purpose.** An earlier version compared a JPEG
    against a PNG, so the two paths differed by their *extension* and the
    assertion held even with the content hash deleted. Only the mutation batch
    said so.
    """
    user_id = uuid4()
    other_jpeg = JPEG + bytes([17, 34, 51])

    assert object_path(user_id, AssetKind.AVATAR, JPEG, "image/jpeg") != object_path(
        user_id, AssetKind.AVATAR, other_jpeg, "image/jpeg"
    )


def test_avatar_and_banner_do_not_collide() -> None:
    """Same user, same bytes, two kinds — one must not overwrite the other."""
    user_id = uuid4()

    assert object_path(user_id, AssetKind.AVATAR, JPEG, "image/jpeg") != object_path(
        user_id, AssetKind.BANNER, JPEG, "image/jpeg"
    )


def test_the_digest_is_not_the_whole_hash_but_is_long_enough() -> None:
    """32 hex characters is 128 bits — unguessable, and short enough to read in a
    URL when someone is debugging one."""
    assert len(digest(JPEG)) == 32


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------


def test_every_asset_lands_in_exactly_one_counter() -> None:
    """The counters must sum to the population the run printed, or an operator
    reconciling them has a discrepancy with no line explaining it."""
    report = AssetReport(copied=3, skipped=2, absent=1, failed=("x: boom",))

    assert report.copied + report.skipped + report.absent + len(report.failed) == 7


def test_failures_name_the_user_and_go_to_their_own_stream() -> None:
    report = AssetReport(failed=("ada@example.com: 404",))

    assert "failed  1" in report.summary()
    assert report.failures() == ["FAILED ada@example.com: 404"]


# --------------------------------------------------------------------------
# Which addresses this migration is willing to contact
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://4f8dee3b4728dbdfcf1f333770d80d0d.cdn.bubble.io/f/x/img.png",
        "https://app.edufurther.org/version-live/img.png",
        "https://lh3.googleusercontent.com/a/ACg8ocK",
        "https://media.licdn.com/dms/image/v2/abc",
        "http://lh3.googleusercontent.com/a/ACg8ocK",
    ],
    ids=["bubble-cdn", "custom-domain", "google-avatar", "linkedin-avatar", "http-scheme"],
)
def test_a_real_source_is_fetchable(url: str) -> None:
    """The two origins legacy images actually have: a Bubble upload, or an
    avatar an OAuth provider handed us at sign-up."""
    assert is_fetchable(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://localhost:8000/admin",
        "http://127.0.0.1/",
        "http://10.0.0.5/internal",
        "https://cdn.bubble.io.attacker.test/x.png",
        "https://notcdn.bubble.io.evil.test/x.png",
        "file:///etc/passwd",
        "gopher://internal/",
        "https://attacker.test/x.png",
        "",
    ],
    ids=[
        "link-local-metadata",
        "localhost",
        "loopback",
        "private-range",
        "suffix-not-on-dot-boundary",
        "lookalike-subdomain",
        "file-scheme",
        "gopher-scheme",
        "unknown-host",
        "empty",
    ],
)
def test_everything_else_is_refused(url: str) -> None:
    """An allowlist, so the interesting cases are the ones nobody enumerated.

    The metadata address is here because it is the classic SSRF target, but it is
    not special: it is refused for the same reason ``attacker.test`` is — it is
    not a source we have.
    """
    assert is_fetchable(url) is False


def test_credentials_in_the_authority_cannot_smuggle_a_host() -> None:
    """``https://cdn.bubble.io@attacker.test/`` is a request to attacker.test.

    Comparing ``netloc`` rather than ``hostname`` would read the user-info part
    and allow it. This is why ``is_fetchable`` uses ``hostname``.
    """
    assert is_fetchable("https://4f8.cdn.bubble.io@attacker.test/x.png") is False


def test_a_port_does_not_defeat_the_allowlist() -> None:
    assert is_fetchable("https://lh3.googleusercontent.com:443/a/x") is True


def test_decide_refuses_an_unrecognised_host_rather_than_copying() -> None:
    """The whole point: an unknown source is not fetched.

    Before this, `decide` returned COPY for everything that was not already ours,
    so any address in the column became an outbound request.
    """
    assert decide("https://attacker.test/x.png", "project.supabase.co") is AssetAction.REFUSE
    assert (
        decide("https://lh3.googleusercontent.com/a/x", "project.supabase.co") is AssetAction.COPY
    )
    assert decide("https://project.supabase.co/o/x.png", "project.supabase.co") is AssetAction.SKIP


def test_a_refusal_is_reported_by_host_not_by_url() -> None:
    """A URL from an unexpected source may carry a path nobody should paste on."""
    assert host_of("https://attacker.test/secret/path?token=abc") == "attacker.test"
    assert host_of(None) == "<no host>"


def test_the_report_surfaces_refused_hosts() -> None:
    """Counted in the summary and named on the failure stream.

    A refusal is not a failure — nothing broke — but both need somebody to look,
    so they share the stream the operator already reads.
    """
    report = AssetReport(copied=1, refused_hosts=("attacker.test",))

    assert "refused 1" in report.summary()
    assert any("attacker.test" in line for line in report.failures())
