"""Social profile links: one canonical form per network (settled decision #182).

**The frontend renders these and never parses them.** So the one place that
knows what a LinkedIn, X or YouTube link looks like is here, and it runs on
both sides of the column: a write stores only the canonical form, and a read
publishes only the canonical form — a legacy value that cannot be made
canonical reads as absent rather than as whatever the old system held.

**An allowlist of hosts, compared exactly.** `https://linkedin.com.evil.com`,
`https://evil.com/?u=linkedin.com` and `https://linkedin.com@evil.com` all
contain the right name somewhere; none of them has it as the host. The URL is
parsed rather than pattern-matched, any userinfo or port refuses it, and the
identity is re-extracted into a URL this module builds — nothing from the input
is echoed except a handle that matched its network's own character set.

Handle rules, as each network documents them:

- **X**: 1-15 of `A-Z a-z 0-9 _`. New handles need 4, but short legacy ones
  exist and still resolve.
- **LinkedIn**: the custom public URL is 3-100 characters of letters, digits and
  hyphens; `_` is also accepted, as company pages use it. **Letters are any
  script's** (owner, 2026-10-18): LinkedIn percent-encodes an accented or
  non-Latin vanity handle (`jos%C3%A9-silva`), and some links carry it decoded
  (`josé-silva`). Either is decoded once as strict UTF-8, NFKC-normalised, and
  must then be letters (`L*`), decimal digits (`Nd`), combining marks (`Mn`,
  `Mc`, never first), `-` or `_`, 3-100 characters; it is stored the way LinkedIn writes it,
  percent-encoded with uppercase hex. A `%` left after decoding is double
  encoding and is refused.
- **YouTube**: a handle is 3-30 of `A-Z a-z 0-9 _ - .`; a channel id is `UC`
  plus 22 of `A-Z a-z 0-9 _ -`. Legacy `/c/` and `/user/` addresses are refused:
  neither maps reliably onto a handle.

Anything after the identifying path segments — `/details/experience`, a tweet's
`/status/…`, a channel's `/videos` — and every query and fragment is dropped.
The link is to the profile, and tracking parameters are not part of it.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qs, quote, unquote, urlsplit
from uuid import UUID

from app.domain.enums import LinkPlatform

__all__ = [
    "LEGACY_LINK_FIELDS",
    "MAX_LENGTH",
    "MAX_WEBSITES",
    "Link",
    "SocialNetwork",
    "canonical_social",
    "handle_of",
    "owned_link",
    "stored_link",
]

#: The longest input considered. The column is `text`; this is the boundary's
#: bound. It must hold the longest canonical link, which is a 100-character
#: LinkedIn slug of 4-byte characters percent-encoded (about 1,233), or a link
#: written and stored would read back as absent.
MAX_LENGTH = 2048

#: How many websites one person may list (Mentor Settings PRD, decision 275);
#: every other platform takes one link.
MAX_WEBSITES = 3

#: The profile field each platform's link stood in for before `user_links`, in
#: the order the Bubble import places them (decision 275). The one copy: the
#: writer clears these columns, the loader and the catch-up read them, and the
#: deprecated response fields are filled from them.
LEGACY_LINK_FIELDS: dict[LinkPlatform, str] = {
    LinkPlatform.X: "social_twitter",
    LinkPlatform.YOUTUBE: "social_youtube",
}


class SocialNetwork(StrEnum):
    LINKEDIN = "linkedin"
    X = "x"
    YOUTUBE = "youtube"
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"
    TIKTOK = "tiktok"
    #: Any site of the user's own: an `http(s)` link on a real host name.
    WEBSITE = "website"


_X_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}", re.ASCII)
_YOUTUBE_HANDLE = re.compile(r"[A-Za-z0-9_.-]{3,30}", re.ASCII)
_YOUTUBE_CHANNEL = re.compile(r"UC[A-Za-z0-9_-]{22}", re.ASCII)
_INSTAGRAM_HANDLE = re.compile(r"[A-Za-z0-9._]{1,30}", re.ASCII)
_FACEBOOK_NAME = re.compile(r"[A-Za-z0-9.]{5,50}", re.ASCII)
_FACEBOOK_ID = re.compile(r"[0-9]{1,20}", re.ASCII)
_TIKTOK_HANDLE = re.compile(r"[A-Za-z0-9._]{2,24}", re.ASCII)
#: A host name of DNS labels with at least one dot, after IDNA encoding.
_WEB_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}", re.ASCII)
#: LinkedIn's country subdomains (`ng.linkedin.com`) are two letters.
_LINKEDIN_HOST = re.compile(r"(?:(?:www|m|[a-z]{2})\.)?linkedin\.com", re.ASCII)

_HOSTS = {
    SocialNetwork.X: frozenset(
        {
            "x.com",
            "www.x.com",
            "mobile.x.com",
            "twitter.com",
            "www.twitter.com",
            "mobile.twitter.com",
        }
    ),
    SocialNetwork.YOUTUBE: frozenset({"youtube.com", "www.youtube.com", "m.youtube.com"}),
    SocialNetwork.INSTAGRAM: frozenset({"instagram.com", "www.instagram.com", "m.instagram.com"}),
    SocialNetwork.FACEBOOK: frozenset(
        {"facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com"}
    ),
    SocialNetwork.TIKTOK: frozenset({"tiktok.com", "www.tiktok.com", "m.tiktok.com"}),
}

#: X paths that fit the handle charset and are not profiles.
_X_RESERVED = frozenset(
    {
        "home", "i", "intent", "share", "search", "explore", "settings", "messages",
        "notifications", "hashtag", "login", "signup", "tos", "privacy", "compose",
    }
)  # fmt: skip

#: Instagram and Facebook paths that fit the handle charset and are not profiles.
_INSTAGRAM_RESERVED = frozenset(
    {"p", "reel", "reels", "explore", "stories", "accounts", "direct", "tv", "about"}
)
_FACEBOOK_RESERVED = frozenset(
    {"pages", "groups", "events", "watch", "marketplace", "share", "login", "home.php", "people",
     "profile.php", "permalink.php", "story.php", "photo.php", "gaming", "help", "settings"}
)  # fmt: skip

#: What each canonical link starts with, so the handle a client shows after its
#: own prefix is read back out of the stored link (`handle_of`).
_PREFIXES: dict[SocialNetwork, tuple[str, ...]] = {
    SocialNetwork.X: ("https://x.com/",),
    SocialNetwork.YOUTUBE: ("https://www.youtube.com/@", "https://www.youtube.com/"),
    SocialNetwork.INSTAGRAM: ("https://www.instagram.com/",),
    SocialNetwork.FACEBOOK: ("https://www.facebook.com/",),
    SocialNetwork.TIKTOK: ("https://www.tiktok.com/@",),
    SocialNetwork.WEBSITE: ("https://",),
    SocialNetwork.LINKEDIN: ("https://www.linkedin.com/in/", "https://www.linkedin.com/"),
}

#: What a website's path and query may carry unescaped; anything else is
#: percent-encoded, so nothing typed is echoed raw.
_PATH_SAFE = "/%:@!$&'()*+,;=-._~"
_QUERY_SAFE = _PATH_SAFE + "?"

_SCHEME = re.compile(r"https?://", re.IGNORECASE | re.ASCII)

#: A LinkedIn slug's characters, by Unicode category, beside `-` and `_`:
#: letters, decimal digits, and combining marks after a base character.
_SLUG_CATEGORIES = ("L", "Nd", "Mn", "Mc")
_MARKS = ("Mn", "Mc")
_SLUG_LENGTH = range(3, 101)


def _linkedin_slug(raw: str) -> str | None:
    """`raw` as LinkedIn writes a slug in a URL, or `None` if it is not one.

    Decoded once as strict UTF-8 (a bad sequence refuses), so a `%` still there
    afterwards is double encoding. **NFKC**, so a decomposed `é` and a
    fullwidth Latin letter land on the slug LinkedIn actually serves rather than on a
    look-alike. Re-encoded with only `A-Z a-z 0-9 - _` unescaped, so nothing
    from the input is echoed beyond a checked slug.
    """
    try:
        text = unicodedata.normalize("NFKC", unquote(raw, errors="strict"))
    except UnicodeDecodeError:
        return None
    if len(text) not in _SLUG_LENGTH or unicodedata.category(text[0]) in _MARKS:
        return None
    for char in text:
        if char not in "-_" and not unicodedata.category(char).startswith(_SLUG_CATEGORIES):
            return None
    return quote(text, safe="-_")


def canonical_social(network: SocialNetwork, value: str | None) -> str | None:
    """The canonical `https://` link for `value` on `network`, or `None`.

    `value` is a bare handle (an `@` in front is fine) or a link on the
    network's own hosts, with or without a scheme. `None` means *not a profile
    link on this network* — the caller decides whether that is a 422 (a write)
    or an absent field (a read of a legacy value).
    """
    if value is None:
        return None
    text = value.strip()
    if not text or len(text) > MAX_LENGTH or not text.isprintable() or " " in text:
        return None

    # A scheme or a path means a link; anything else is a handle, and must then
    # match its network's own character set.
    if network is SocialNetwork.WEBSITE:
        return _website(text)
    if _SCHEME.match(text) or "/" in text:
        return _from_url(network, text)
    return _from_handle(network, text.removeprefix("@"))


def handle_of(network: SocialNetwork, url: str) -> str:
    """What follows the client's prefix in a canonical link: the handle, or for
    a website the address without `https://`. Read back out of the stored link,
    so the two can never disagree."""
    for prefix in _PREFIXES[network]:
        if url.startswith(prefix):
            return url[len(prefix) :]
    return url


def _website(text: str) -> str | None:
    """`https://` plus a real host name, keeping the path and query.

    Only `http` and `https`, typed or implied; the link is always published as
    `https`, which is what the client shows. Userinfo and ports are refused as
    for every other network, and the host must be DNS labels with a dot, so
    `javascript:`, `localhost` and a bare word all fail. The fragment is dropped.
    """
    if not _SCHEME.match(text):
        # A scheme of any other kind (`javascript:`, `mailto:`) is a colon in
        # the authority, which ends at the first `/`, `?` or `#`.
        if ":" in re.split(r"[/?#]", text, maxsplit=1)[0]:
            return None
        text = f"https://{text}"
    try:
        parts = urlsplit(text)
        port = parts.port
        host = (parts.hostname or "").rstrip(".").encode("idna").decode("ascii")
    except ValueError, UnicodeError:
        return None
    if "@" in parts.netloc or port is not None or not _WEB_HOST.fullmatch(host):
        return None
    path = quote(parts.path, safe=_PATH_SAFE)
    query = f"?{quote(parts.query, safe=_QUERY_SAFE)}" if parts.query else ""
    link = f"https://{host}{path}{query}"
    return link if len(link) <= MAX_LENGTH else None


def _from_handle(network: SocialNetwork, handle: str) -> str | None:
    if network is SocialNetwork.X:
        if _X_HANDLE.fullmatch(handle) and handle.lower() not in _X_RESERVED:
            return f"https://x.com/{handle}"
        return None
    if network is SocialNetwork.LINKEDIN:
        slug = _linkedin_slug(handle)
        return f"https://www.linkedin.com/in/{slug}" if slug else None
    if network is SocialNetwork.INSTAGRAM:
        if (
            _INSTAGRAM_HANDLE.fullmatch(handle)
            and handle.lower() not in _INSTAGRAM_RESERVED
            and not handle.startswith(".")
            and not handle.endswith(".")
        ):
            return f"https://www.instagram.com/{handle}"
        return None
    if network is SocialNetwork.FACEBOOK:
        if handle.startswith("profile.php?"):
            # The value a client shows for a profile with no username.
            return _from_url(network, f"https://www.facebook.com/{handle}")
        if _FACEBOOK_NAME.fullmatch(handle) and handle.lower() not in _FACEBOOK_RESERVED:
            return f"https://www.facebook.com/{handle}"
        return None
    if network is SocialNetwork.TIKTOK:
        if _TIKTOK_HANDLE.fullmatch(handle) and not handle.endswith("."):
            return f"https://www.tiktok.com/@{handle}"
        return None
    if _YOUTUBE_HANDLE.fullmatch(handle):
        return f"https://www.youtube.com/@{handle}"
    return None


def _from_url(network: SocialNetwork, text: str) -> str | None:
    if not _SCHEME.match(text):
        # `linkedin.com/in/ada` — a link typed without its scheme.
        text = f"https://{text}"
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return None
    # Userinfo is how `https://linkedin.com@evil.com` points elsewhere; a port
    # is never part of a profile link. Either one refuses the value outright.
    if "@" in parts.netloc or port is not None or parts.hostname is None:
        return None
    host = parts.hostname.rstrip(".")
    segments = [s for s in parts.path.split("/") if s]

    if network is SocialNetwork.LINKEDIN:
        if not _LINKEDIN_HOST.fullmatch(host) or len(segments) < 2:
            return None
        kind, slug = segments[0].lower(), _linkedin_slug(segments[1])
        if slug and kind in ("in", "company"):
            return f"https://www.linkedin.com/{kind}/{slug}"
        return None

    if host not in _HOSTS[network] or not segments:
        return None
    if network in (SocialNetwork.X, SocialNetwork.INSTAGRAM):
        return _from_handle(network, segments[0])
    if network is SocialNetwork.FACEBOOK:
        # `profile.php?id=…` is how Facebook links a profile with no username.
        # `/people/<Name>/<id>` is the same profile, by id.
        if segments[0] == "people":
            if len(segments) >= 3 and _FACEBOOK_ID.fullmatch(segments[2]):
                return f"https://www.facebook.com/profile.php?id={segments[2]}"
            return None
        if segments[0] == "profile.php":
            ids = parse_qs(parts.query).get("id", [])
            if len(ids) == 1 and _FACEBOOK_ID.fullmatch(ids[0]):
                return f"https://www.facebook.com/profile.php?id={ids[0]}"
            return None
        return _from_handle(network, segments[0])
    if network is SocialNetwork.TIKTOK:
        first = segments[0]
        return _from_handle(network, first[1:]) if first.startswith("@") else None
    first = segments[0]
    if first.startswith("@"):
        return _from_handle(network, first[1:])
    if first == "channel" and len(segments) > 1 and _YOUTUBE_CHANNEL.fullmatch(segments[1]):
        return f"https://www.youtube.com/channel/{segments[1]}"
    return None


@dataclass(frozen=True, slots=True)
class Link:
    """One `user_links` row as every reader publishes it: the canonical `url`,
    and the `value` a client shows after its own prefix, read back out of it."""

    id: UUID
    platform: str
    url: str
    value: str
    position: int


def stored_link(link_id: UUID, platform: str, url: str, position: int) -> Link | None:
    """A stored row through the read rule (#182): one it cannot read is absent."""
    network = SocialNetwork(platform)
    canonical = canonical_social(network, url)
    if canonical is None:
        return None
    return Link(
        id=link_id,
        platform=platform,
        url=canonical,
        value=handle_of(network, canonical),
        position=position,
    )


def owned_link(link_id: UUID, platform: str, url: str, position: int) -> Link:
    """A stored row as its owner sees it: canonical where the rule reads it, and
    as stored where it no longer does, so the owner can still see and delete a
    link that holds a platform's slot. Never published to anyone else (#182);
    the column's `CHECK` keeps it an `https://` link either way."""
    link = stored_link(link_id, platform, url, position)
    if link is not None:
        return link
    return Link(
        id=link_id,
        platform=platform,
        url=url,
        value=handle_of(SocialNetwork(platform), url),
        position=position,
    )
