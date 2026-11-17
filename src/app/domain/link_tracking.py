"""Mentor profile link tracking: what a visit or a share records (decisions 346, 347).

Pure: the client's hints are normalised here, and the visitor key is computed
here, so the rules are tested without a request or a database. The writes are
`infra/db/link_tracking`; the endpoints are `POST /mentors/{handle}/visits`
and `/shares`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import re
from urllib.parse import urlsplit
from uuid import UUID

from app.domain.enums import LinkVia

#: How long a visitor key is: one HMAC-SHA256 digest.
VISITOR_KEY_BYTES = 32
#: How long a day's random key is (decision 348), the HMAC-SHA256 output size.
DAY_KEY_BYTES = 32
#: The longest host name DNS allows; a longer referrer is dropped, not cut.
MAX_REFERRER_HOST = 253

#: One DNS label: letters, digits and inner hyphens, at most 63 characters.
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
#: A host name with at least one dot (`wa.me`, `l.instagram.com`).
_HOST = re.compile(rf"{_LABEL}(?:\.{_LABEL})+")


def normalise_via(raw: str | None) -> LinkVia | None:
    """The channel a link was shared through, or ``None`` for anything that is
    not one of ours: an unknown value is dropped, never refused, so a link
    edited by hand still counts as a visit."""
    if raw is None:
        return None
    try:
        return LinkVia(raw.strip().lower())
    except ValueError:
        return None


def normalise_referrer_host(raw: str | None) -> str | None:
    """The bare host a visitor came from, or ``None``.

    Lowercased, with any scheme, credentials, path, port, trailing dot and a
    leading ``www.`` taken off. Only a host name survives: an address, a single
    label (`localhost`), anything over `MAX_REFERRER_HOST` characters or
    anything with characters a host cannot carry is ``None``, so the column
    never holds free text a visitor typed.
    """
    if raw is None:
        return None
    value = raw.strip().lower()
    if not value:
        return None
    if "//" not in value:
        value = f"//{value}"
    try:
        host = urlsplit(value).hostname
    except ValueError:
        return None
    if host is None:
        return None
    host = host.rstrip(".")
    host = host.removeprefix("www.")
    if len(host) > MAX_REFERRER_HOST or not _HOST.fullmatch(host):
        return None
    if all(label.isdigit() for label in host.split(".")):
        return None  # an IPv4 address names a machine, not a site
    return host


def member_identity(user_id: UUID) -> str:
    """Who a signed-in visitor is, for `visitor_key`: their user id."""
    return f"user:{user_id}"


def guest_identity(address: str, user_agent: str) -> str:
    """Who a guest is, for `visitor_key`: their address and browser, the
    cookieless pattern's pair. Either may be empty."""
    return f"guest:{address}\n{user_agent}"


def visitor_key(secret: bytes, day: dt.date, mentor_id: UUID, identity: str) -> bytes:
    """The 32-byte key one visitor has for one mentor on one UTC day.

    HMAC-SHA256 under a key derived from ``secret`` and ``day``, so yesterday's
    keys cannot be matched with today's, over the mentor and the identity, so
    one visitor's keys for two mentors cannot be matched either. Nothing about
    the visitor can be read back from it without the secret.
    """
    daily = hmac.new(secret, day.isoformat().encode(), hashlib.sha256).digest()
    message = f"{mentor_id}\n{identity}".encode()
    return hmac.new(daily, message, hashlib.sha256).digest()
