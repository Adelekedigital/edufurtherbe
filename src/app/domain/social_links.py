"""Social profile links: one canonical form per network (settled decision #181).

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
  hyphens; `_` is also accepted, as company pages use it. A slug with non-ASCII
  characters is refused (it arrives percent-encoded) — rare, and the cost of
  refusing is a link not shown, never a wrong one.
- **YouTube**: a handle is 3-30 of `A-Z a-z 0-9 _ - .`; a channel id is `UC`
  plus 22 of `A-Z a-z 0-9 _ -`. Legacy `/c/` and `/user/` addresses are refused:
  neither maps reliably onto a handle.

Anything after the identifying path segments — `/details/experience`, a tweet's
`/status/…`, a channel's `/videos` — and every query and fragment is dropped.
The link is to the profile, and tracking parameters are not part of it.
"""

from __future__ import annotations

import re
from enum import StrEnum
from urllib.parse import urlsplit

__all__ = ["MAX_LENGTH", "SocialNetwork", "canonical_social"]

#: The longest input considered. The column is `text`; this is the boundary's
#: bound, and a longer value is not a profile link on any of the three.
MAX_LENGTH = 500


class SocialNetwork(StrEnum):
    LINKEDIN = "linkedin"
    X = "x"
    YOUTUBE = "youtube"


_X_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}", re.ASCII)
_LINKEDIN_SLUG = re.compile(r"[A-Za-z0-9_-]{3,100}", re.ASCII)
_YOUTUBE_HANDLE = re.compile(r"[A-Za-z0-9_.-]{3,30}", re.ASCII)
_YOUTUBE_CHANNEL = re.compile(r"UC[A-Za-z0-9_-]{22}", re.ASCII)
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
}

#: X paths that fit the handle charset and are not profiles.
_X_RESERVED = frozenset(
    {
        "home", "i", "intent", "share", "search", "explore", "settings", "messages",
        "notifications", "hashtag", "login", "signup", "tos", "privacy", "compose",
    }
)  # fmt: skip

_SCHEME = re.compile(r"https?://", re.IGNORECASE | re.ASCII)


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
    if _SCHEME.match(text) or "/" in text:
        return _from_url(network, text)
    return _from_handle(network, text.removeprefix("@"))


def _from_handle(network: SocialNetwork, handle: str) -> str | None:
    if network is SocialNetwork.X:
        if _X_HANDLE.fullmatch(handle) and handle.lower() not in _X_RESERVED:
            return f"https://x.com/{handle}"
        return None
    if network is SocialNetwork.LINKEDIN:
        return f"https://www.linkedin.com/in/{handle}" if _LINKEDIN_SLUG.fullmatch(handle) else None
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
        kind, slug = segments[0].lower(), segments[1]
        if kind == "in" and _LINKEDIN_SLUG.fullmatch(slug):
            return f"https://www.linkedin.com/in/{slug}"
        if kind == "company" and _LINKEDIN_SLUG.fullmatch(slug):
            return f"https://www.linkedin.com/company/{slug}"
        return None

    if host not in _HOSTS[network] or not segments:
        return None
    if network is SocialNetwork.X:
        return _from_handle(network, segments[0])
    first = segments[0]
    if first.startswith("@"):
        return _from_handle(network, first[1:])
    if first == "channel" and len(segments) > 1 and _YOUTUBE_CHANNEL.fullmatch(segments[1]):
        return f"https://www.youtube.com/channel/{segments[1]}"
    return None
