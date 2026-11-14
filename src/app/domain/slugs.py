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
from collections.abc import Iterator
from itertools import count
from uuid import UUID

from app.domain.handles import RESERVED_HANDLES, fold_to_handle, strip_marks

#: The column CHECK's bound (`slug_is_url_safe`), and the agreed maximum.
MAX_SLUG_LENGTH = 60


#: The frontend's handle check for `/mentors/{handle}`. Every handle this
#: module makes fits it; an imported one that does not is reported.
HANDLE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

PLACEHOLDER_PREFIX = "user-"
_TOKEN = re.compile(r"^[0-9a-z]{8}$")


def strip_accents(value: str) -> str | None:
    """``value`` with its accents removed, or ``None`` if a character has no
    ASCII form (``ø``, ``ß``, ``山``). Decision 311's rule, shared with the
    import's `fold_slug`, which must never alter a handle beyond its accents."""
    bare = strip_marks(value)
    return bare if bare.isascii() else None


def looks_like_id(handle: str) -> bool:
    """Would ``/mentors/{handle}`` read this as an id? Asked exactly as the
    resolver asks it (`UUID(handle)`), which also takes 32 hex digits with
    the hyphens anywhere or none."""
    try:
        UUID(handle)
    except ValueError:
        return False
    return True


def name_slug(first_name: str | None, last_name: str | None) -> str | None:
    """A handle from a name, or ``None`` when the name gives nothing usable.

    The shared fold (`domain.handles.fold_to_handle`): accents fold away
    (Chloé → chloe) and any other run of characters becomes one ``-``. Cut to the limit at a
    ``-`` where there is one. A UUID-shaped result is refused.
    """
    slug = fold_to_handle(" ".join(part for part in (first_name, last_name) if part))
    if len(slug) > MAX_SLUG_LENGTH:
        cut = slug[: MAX_SLUG_LENGTH + 1]
        slug = cut[: cut.rfind("-")] if "-" in cut else slug[:MAX_SLUG_LENGTH]
        slug = slug.strip("-")
    if not slug or looks_like_id(slug):
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
    if base not in RESERVED_HANDLES and not looks_like_id(base):
        yield base
    for n in count(2):
        suffix = f"-{n}"
        option = base[: MAX_SLUG_LENGTH - len(suffix)].rstrip("-") + suffix
        if not looks_like_id(option):
            yield option


def is_servable(slug: str) -> bool:
    """Can ``/mentors/{slug}`` reach this handle? It fits the frontend's
    pattern, is not a bare route word, and is not shaped like an id."""
    return (
        bool(HANDLE_PATTERN.match(slug))
        and slug not in RESERVED_HANDLES
        and not looks_like_id(slug)
    )
