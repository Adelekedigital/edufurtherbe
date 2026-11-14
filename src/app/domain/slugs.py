"""Public profile handles: ``/mentors/{handle}`` (owner, 2026-11-14).

Every account has one. An account is created at first sign-in, before it has
a name, so it starts with a placeholder (``user-`` and 8 random base36
characters); the first time a first name is saved, the placeholder is replaced
once by a handle made from the name. After that a handle never changes, even
when the name does: links to it are shared. Imported handles are kept as they
were (decision 311 folds only their accents).

The frontend resolves ``/mentors/{handle}`` as an id or a slug, so a handle
fits ``^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`` and is never shaped like a UUID.
Uniqueness is the database's: ``ix_users_slug_live`` plus the lowercase CHECK,
tried in the order `candidates` gives.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator
from itertools import count

#: The column CHECK's bound (`slug_is_url_safe`), and the agreed maximum.
MAX_SLUG_LENGTH = 60

#: Route words a handle may never be on its own (owner and frontend, 14 Nov).
RESERVED_SLUGS = frozenset(
    {
        "me", "new", "edit", "admin", "settings", "api", "explore",
        "invite", "mentors", "mentees", "null", "undefined",
    }
)  # fmt: skip

#: The frontend's handle check for `/mentors/{handle}`. Every handle this
#: module makes fits it; an imported one that does not is reported.
HANDLE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

PLACEHOLDER_PREFIX = "user-"
_TOKEN = re.compile(r"^[0-9a-z]{8}$")
_UUID_SHAPE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SEPARATORS = re.compile(r"[^a-z0-9]+")


def strip_accents(value: str) -> str | None:
    """``value`` with its accents removed, or ``None`` if a character has no
    ASCII form (``ø``, ``ß``, ``山``). Decision 311's rule, shared with the
    import's `fold_slug`, which must never alter a handle beyond its accents."""
    decomposed = unicodedata.normalize("NFD", value)
    if any(not ch.isascii() and not unicodedata.combining(ch) for ch in decomposed):
        return None
    return "".join(ch for ch in decomposed if ch.isascii())


def name_slug(first_name: str | None, last_name: str | None) -> str | None:
    """A handle from a name, or ``None`` when the name gives nothing usable.

    Accents fold away (Chloé → chloe); any other run of characters, a letter
    with no ASCII form included, becomes one ``-``. Cut to the limit at a
    ``-`` where there is one. A UUID-shaped result is refused.
    """
    words = " ".join(part for part in (first_name, last_name) if part)
    ascii_only = "".join(strip_accents(ch) or " " for ch in unicodedata.normalize("NFC", words))
    slug = _SEPARATORS.sub("-", ascii_only.lower()).strip("-")
    if len(slug) > MAX_SLUG_LENGTH:
        cut = slug[: MAX_SLUG_LENGTH + 1]
        slug = cut[: cut.rfind("-")] if "-" in cut else slug[:MAX_SLUG_LENGTH]
        slug = slug.strip("-")
    if not slug or _UUID_SHAPE.match(slug):
        return None
    return slug


def placeholder_slug(token: str) -> str:
    """``user-<token>``; the token is 8 random lowercase base36 characters."""
    if not _TOKEN.match(token):
        raise ValueError("a placeholder token is 8 lowercase base36 characters")
    return f"{PLACEHOLDER_PREFIX}{token}"


def candidates(base: str) -> Iterator[str]:
    """``base``, then ``base-2``, ``base-3``…, each within the limit.

    A reserved word is never offered on its own, so ``admin`` starts at
    ``admin-2``.
    """
    if base not in RESERVED_SLUGS:
        yield base
    for n in count(2):
        suffix = f"-{n}"
        yield base[: MAX_SLUG_LENGTH - len(suffix)].rstrip("-") + suffix
