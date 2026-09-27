"""`canonical_social`: what a stored or published social link may be (#181)."""

from __future__ import annotations

import pytest

from app.domain.social_links import MAX_LENGTH, SocialNetwork, canonical_social

LI, X, YT = SocialNetwork.LINKEDIN, SocialNetwork.X, SocialNetwork.YOUTUBE
CHANNEL = "UC" + "a1B2_c3-D4e5F6g7H8i9J0"

ACCEPTED = [
    # LinkedIn: handle, every host spelling, company pages, trailing noise dropped
    (LI, "ada-lovelace", "https://www.linkedin.com/in/ada-lovelace"),
    (LI, "@ada-lovelace", "https://www.linkedin.com/in/ada-lovelace"),
    (LI, "https://www.linkedin.com/in/ada-lovelace", "https://www.linkedin.com/in/ada-lovelace"),
    (LI, "http://linkedin.com/in/ada-lovelace/", "https://www.linkedin.com/in/ada-lovelace"),
    (LI, "linkedin.com/in/ada-lovelace", "https://www.linkedin.com/in/ada-lovelace"),
    (LI, "HTTPS://NG.LINKEDIN.COM/in/ada", "https://www.linkedin.com/in/ada"),
    (LI, "https://m.linkedin.com/in/ada?trk=share#top", "https://www.linkedin.com/in/ada"),
    (LI, "https://www.linkedin.com/in/ada/details/experience/", "https://www.linkedin.com/in/ada"),
    (
        LI,
        "https://www.linkedin.com/company/edu_further",
        "https://www.linkedin.com/company/edu_further",
    ),
    (LI, "  ada-lovelace  ", "https://www.linkedin.com/in/ada-lovelace"),
    # X: both domains, mobile, a tweet link resolves to its author
    (X, "ada", "https://x.com/ada"),
    (X, "@Ada_99", "https://x.com/Ada_99"),
    (X, "https://twitter.com/ada", "https://x.com/ada"),
    (X, "https://mobile.twitter.com/ada?s=20", "https://x.com/ada"),
    (X, "www.x.com/ada", "https://x.com/ada"),
    (X, "https://x.com/ada/status/1234567890", "https://x.com/ada"),
    (X, "x" * 15, "https://x.com/" + "x" * 15),
    # YouTube: handle, dotted handle, channel id, tabs dropped
    (YT, "ada.codes", "https://www.youtube.com/@ada.codes"),
    (YT, "@ada-codes", "https://www.youtube.com/@ada-codes"),
    (YT, "https://youtube.com/@ada_codes/videos", "https://www.youtube.com/@ada_codes"),
    (YT, "https://m.youtube.com/@ada", "https://www.youtube.com/@ada"),
    (
        YT,
        f"https://www.youtube.com/channel/{CHANNEL}",
        f"https://www.youtube.com/channel/{CHANNEL}",
    ),
]

REFUSED = [
    # the hostile ones: the right name, never as the host
    (LI, "https://linkedin.com.evil.com/in/ada"),
    (LI, "https://evil.com/?u=linkedin.com/in/ada"),
    (LI, "https://linkedin.com@evil.com/in/ada"),
    (LI, "https://evil.com/linkedin.com/in/ada"),
    (LI, "https://evillinkedin.com/in/ada"),
    (LI, "https://www.linkedin.com:8443/in/ada"),
    (LI, "javascript:alert(1)//linkedin.com/in/ada"),
    (LI, "ftp://linkedin.com/in/ada"),
    (X, "https://x.com.evil.com/ada"),
    (X, "https://ada@x.com/ada"),
    (X, "https://xx.com/ada"),
    (YT, "https://youtube.com.evil.com/@ada"),
    (YT, "https://youtu.be/@ada"),
    # the right host, not a profile
    (LI, "https://www.linkedin.com/"),
    (LI, "https://www.linkedin.com/feed/update/123"),
    (LI, "https://www.linkedin.com/pub/ada/1/2/3"),
    (LI, "https://www.linkedin.com/in/%2e%2e"),
    (LI, "https://www.linkedin.com/in/jos%C3%A9"),
    (X, "https://x.com/"),
    (X, "https://x.com/home"),
    (X, "https://twitter.com/i/lists/1"),
    (X, "https://x.com/intent/tweet?text=hi"),
    (YT, "https://www.youtube.com/watch?v=abc"),
    (YT, "https://www.youtube.com/c/adacodes"),
    (YT, "https://www.youtube.com/user/adacodes"),
    (YT, "https://www.youtube.com/channel/UCshort"),
    # handles outside each network's own character set or length
    (X, "x" * 16),
    (X, "ada.lovelace"),
    (X, "ada-lovelace"),
    (LI, "ad"),
    (LI, "ada lovelace"),
    (LI, "ada.lovelace"),
    (YT, "ad"),
    (YT, "a" * 31),
    (YT, "ada!"),
    (LI, "ada\tlovelace"),
    (LI, "ada\x00"),
    (LI, "a" * (MAX_LENGTH + 1)),
    # `urlsplit` silently deletes tabs and newlines, so these would parse as the
    # real host; refusing unprintable input is what stops them
    (LI, "https://linked\tin.com/in/ada"),
    (X, "https://x.\ncom/ada"),
    # a link past the bound is refused even where only its query is long
    (LI, "https://www.linkedin.com/in/ada?" + "q" * MAX_LENGTH),
    (X, ""),
    (X, "   "),
    (X, "@"),
]


@pytest.mark.parametrize(("network", "value", "expected"), ACCEPTED)
def test_a_profile_link_becomes_its_canonical_form(
    network: SocialNetwork, value: str, expected: str
) -> None:
    assert canonical_social(network, value) == expected


@pytest.mark.parametrize(("network", "value"), REFUSED)
def test_anything_else_is_not_a_profile_link(network: SocialNetwork, value: str) -> None:
    assert canonical_social(network, value) is None


def test_none_is_absent() -> None:
    assert canonical_social(X, None) is None


def test_a_link_on_one_network_is_not_a_link_on_another() -> None:
    assert canonical_social(X, "https://www.linkedin.com/in/ada") is None
    assert canonical_social(LI, "https://x.com/ada") is None
    assert canonical_social(YT, "https://twitter.com/ada") is None


@pytest.mark.parametrize(
    ("network", "expected"), [(network, expected) for network, _, expected in ACCEPTED]
)
def test_the_canonical_form_is_a_fixed_point(network: SocialNetwork, expected: str) -> None:
    """A stored value is read back through the same rule — it must survive it."""
    assert canonical_social(network, expected) == expected
