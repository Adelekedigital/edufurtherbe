"""The handle alphabet, shared by invite codes and profile handles (rule 8).

One fold from a name to ``[a-z0-9-]``, one random alphabet, and one list of
route words neither may be: a second copy of any of these drifts (it had
already: the two folds disagreed on ``Søren``, the two lists on ``login``).
"""

from __future__ import annotations

import re
import secrets
import string
import unicodedata

#: Lowercase letters and digits: the random part of a default invite code and
#: of a placeholder profile handle.
HANDLE_ALPHABET = string.ascii_lowercase + string.digits

_NOT_HANDLE = re.compile(r"[^a-z0-9]+")

#: Route and platform words no handle may be on its own: an invite code or a
#: profile handle that read as one of these would pass for an official page.
#: The profile list the frontend asked for (14 Nov) is part of it.
RESERVED_HANDLES = frozenset(
    {
        "about", "account", "admin", "administrator", "api", "app", "auth",
        "contact", "dashboard", "edit", "edufurther", "explore", "help", "home",
        "invite", "invites", "login", "logout", "me", "mentees", "mentor",
        "mentors", "nan", "new", "null", "official", "privacy", "referral",
        "referrals", "root", "security", "settings", "signin", "signup", "staff",
        "support", "system", "team", "terms", "test", "undefined",
    }
)  # fmt: skip


def strip_marks(text: str) -> str:
    """``text`` decomposed (NFKD) with its combining marks dropped: ``Zoë`` to
    ``Zoe``, ``ﬁ`` to ``fi``. The one decomposition every fold here builds on:
    handles, the import's accent rule and search (rule 8)."""
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )


def fold_to_handle(text: str) -> str:
    """``text`` as lowercase ``[a-z0-9]`` runs joined by one ``-``.

    NFKD then dropping the combining marks takes ``Zoë`` to ``zoe``; any other
    character (``ø``, ``山``, punctuation, space) separates. Never starts or
    ends with ``-``; empty when nothing usable is left.
    """
    ascii_text = "".join(ch if ch.isascii() else " " for ch in strip_marks(text))
    return _NOT_HANDLE.sub("-", ascii_text.lower()).strip("-")


def random_token(length: int) -> str:
    """``length`` characters of `HANDLE_ALPHABET`, from ``secrets``."""
    return "".join(secrets.choice(HANDLE_ALPHABET) for _ in range(length))
