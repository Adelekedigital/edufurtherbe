"""The Mentor Settings platforms: Instagram, Facebook, TikTok and websites (decision 275).

The same contract as `test_social_links.py` for X, YouTube and LinkedIn: a
handle or a link on the network's own hosts becomes one canonical `https://`
link, anything else is refused, and the canonical form is a fixed point. Plus
`handle_of`, which reads the client's value back out of the stored link.
"""

from __future__ import annotations

import pytest

from app.domain.enums import LinkPlatform
from app.domain.social_links import MAX_LENGTH, SocialNetwork, canonical_social, handle_of

IG, FB, TT, WEB = (
    SocialNetwork.INSTAGRAM,
    SocialNetwork.FACEBOOK,
    SocialNetwork.TIKTOK,
    SocialNetwork.WEBSITE,
)

ACCEPTED = [
    (IG, "ada.l", "https://www.instagram.com/ada.l"),
    (IG, "@ada_l", "https://www.instagram.com/ada_l"),
    (IG, "instagram.com/ada.l/", "https://www.instagram.com/ada.l"),
    (IG, "https://m.instagram.com/ada?igsh=xyz", "https://www.instagram.com/ada"),
    (FB, "ada.lovelace", "https://www.facebook.com/ada.lovelace"),
    (FB, "https://web.facebook.com/ada.lovelace/about", "https://www.facebook.com/ada.lovelace"),
    (
        FB,
        "https://m.facebook.com/profile.php?id=123456&ref=x",
        "https://www.facebook.com/profile.php?id=123456",
    ),
    (TT, "@ada_l", "https://www.tiktok.com/@ada_l"),
    (TT, "ada.l", "https://www.tiktok.com/@ada.l"),
    (TT, "tiktok.com/@ada_l/video/1", "https://www.tiktok.com/@ada_l"),
    (WEB, "ada.dev", "https://ada.dev"),
    (WEB, "http://Ada.Dev/blog?x=1#top", "https://ada.dev/blog?x=1"),
    (WEB, "https://medium.com/@ada/post", "https://medium.com/@ada/post"),
    (WEB, "https://bücher.de/x", "https://xn--bcher-kva.de/x"),
    (WEB, "ada.substack.com/p/hello", "https://ada.substack.com/p/hello"),
]

REFUSED = [
    (IG, ".ada"),
    (IG, "ada."),
    (IG, "instagram.com/p/xyz"),
    (IG, "a" * 31),
    (IG, "https://instagram.com.evil.com/ada"),
    (FB, "fb"),
    (FB, "profile.php"),
    (FB, "facebook.com/groups/abc"),
    (FB, "https://facebook.com/profile.php?id=abc"),
    (FB, "https://facebook.com/profile.php"),
    (TT, "a"),
    (TT, "tiktok.com/ada"),
    (TT, "@ada."),
    (WEB, "javascript:alert(1)"),
    (WEB, "data:text/html,hi"),
    (WEB, "ftp://ada.dev"),
    (WEB, "localhost"),
    (WEB, "ada"),
    (WEB, "https://u:p@ada.dev"),
    (WEB, "https://ada.dev:8080/"),
    (WEB, "ada.dev/a b"),
    (WEB, "ada.dev/\x00"),
    (WEB, "https://ada.dev/" + "a" * MAX_LENGTH),
    (WEB, ""),
]


@pytest.mark.parametrize(("network", "value", "expected"), ACCEPTED)
def test_a_profile_link_becomes_its_canonical_form(
    network: SocialNetwork, value: str, expected: str
) -> None:
    assert canonical_social(network, value) == expected


@pytest.mark.parametrize(("network", "value"), REFUSED)
def test_anything_else_is_refused(network: SocialNetwork, value: str) -> None:
    assert canonical_social(network, value) is None


@pytest.mark.parametrize(("network", "expected"), [(n, e) for n, _, e in ACCEPTED])
def test_the_canonical_form_is_a_fixed_point(network: SocialNetwork, expected: str) -> None:
    """A stored link read back through the rule must come out unchanged, or a
    read would publish something other than what was written."""
    assert canonical_social(network, expected) == expected


@pytest.mark.parametrize(
    ("network", "url", "value"),
    [
        (SocialNetwork.X, "https://x.com/ada", "ada"),
        (SocialNetwork.YOUTUBE, "https://www.youtube.com/@ada", "ada"),
        (
            SocialNetwork.YOUTUBE,
            "https://www.youtube.com/channel/UC" + "a" * 22,
            "channel/UC" + "a" * 22,
        ),
        (IG, "https://www.instagram.com/ada.l", "ada.l"),
        (FB, "https://www.facebook.com/profile.php?id=1", "profile.php?id=1"),
        (TT, "https://www.tiktok.com/@ada_l", "ada_l"),
        (WEB, "https://ada.dev/blog", "ada.dev/blog"),
    ],
)
def test_the_value_is_what_follows_the_clients_prefix(
    network: SocialNetwork, url: str, value: str
) -> None:
    assert handle_of(network, url) == value


@pytest.mark.parametrize(("network", "value", "_expected"), ACCEPTED)
def test_the_value_typed_back_gives_the_same_link(
    network: SocialNetwork, value: str, _expected: str
) -> None:
    """What the settings page shows is what it would send: no drift on a resave."""
    url = canonical_social(network, value)
    assert url is not None
    assert canonical_social(network, handle_of(network, url)) == url


def test_every_link_platform_has_a_rule() -> None:
    """The column's vocabulary and the rule's networks must agree (#8)."""
    assert {p.value for p in LinkPlatform} <= {n.value for n in SocialNetwork}
    assert SocialNetwork.LINKEDIN.value not in {p.value for p in LinkPlatform}
