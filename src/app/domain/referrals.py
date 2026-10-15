"""Invite codes, and when an invite counts.

Pure, and here rather than in a store because both rules guard a **recurring**
grant. The starter is one credit once; an unlock opens three a month
indefinitely, which is why the referral gate is the stricter of the two and why
its predicate is written where it can be read and tested on its own.

WHAT QUALIFIES, AND WHY IT CHANGED
==================================
**The invitee completing their profile.** Settled decision 20 originally said
"signup plus a verified email", recorded as deliberately temporary, with
*invitee completes their profile* named as the target.

It could not ship as written. ``users.email_verified_at`` is written by exactly
one thing — the ETL, for migrated users — and ``TokenClaims`` carries only
``subject`` and ``email``, never ``email_verified``. For any new user the column
stays null forever, so ``qualified_at`` would never be set and the unlock gate
could never open: a recurring benefit gated behind a condition nothing can
satisfy.

PR 4 built onboarding completion, which made the *target* predicate cheap. So
this is the target arriving early rather than a new decision, and it is strictly
stronger than what it replaces: finishing a profile is work, clicking a
verification link is not. Amended in ADR 0027.
"""

from __future__ import annotations

import re
import secrets
from datetime import timedelta
from typing import Literal

__all__ = [
    "CODE_LENGTH",
    "HANDLE_MAX_LENGTH",
    "HANDLE_MIN_LENGTH",
    "MAX_CODE_LENGTH",
    "NO_INVITE",
    "RESERVED_HANDLES",
    "RETIRED_CODE_HOLD",
    "handle_problem",
    "is_plausible_code",
    "make_code",
    "may_qualify",
    "normalise_handle",
]

#: Bytes of entropy, not characters — ``token_urlsafe`` base64-encodes, so the
#: string is longer than this. Sixteen bytes is 128 bits, which is the point at
#: which guessing stops being a strategy.
#:
#: **The code is a bearer token that travels in an email.** Anybody holding one
#: can attach themselves to somebody else's invite, so being infeasible to guess
#: is its only protection.
CODE_BYTES = 16

#: The shortest a generated code can be. ``token_urlsafe(16)`` returns 22
#: characters; asserted as a floor rather than an equality so the entropy can be
#: raised without breaking a test that was never about the exact length.
CODE_LENGTH = 22


def make_code() -> str:
    """A fresh invite code.

    ``secrets``, never ``random``: the latter is seeded predictably and is
    documented as unsuitable for anything security-sensitive. URL-safe because
    it travels in a link, and ``+`` or ``/`` do not survive a query string
    unescaped — an invite that breaks on paste is an invite nobody uses.

    Uniqueness is **not** promised here; ``uq_referrals_code`` is what promises
    it. At 128 bits a collision is not a case worth writing code for, but it is
    a case worth having the database refuse rather than silently accept.
    """
    return secrets.token_urlsafe(CODE_BYTES)


def may_qualify(*, has_invitee: bool, already_qualified: bool) -> bool:
    """Whether this referral should be marked qualified now.

    Both conditions, and the second is not defensive padding: onboarding
    completion is idempotent and this predicate is therefore asked more than
    once for the same referral. Without it a repeat would move ``qualified_at``
    and — the part that costs money — invite a second unlock grant.

    Keyword-only, because two booleans in a row is exactly the signature where a
    caller transposes the arguments and every test still passes.
    """
    return has_invitee and not already_qualified


#: The one not-found sentence for a code, from claim and the landing lookup alike.
NO_INVITE = "no invite carries that code"

#: The longest code anybody may present, whether to claim or to look up.
#: Generated codes are 22 characters and handles at most 30; the ceiling is
#: generous rather than exact, so raising the entropy breaks no client.
MAX_CODE_LENGTH = 128

#: Every code is drawn from the URL-safe alphabet: ``token_urlsafe`` output and
#: handles alike. Anything else cannot be a code, so the public lookup answers it
#: without asking the database.
_PLAUSIBLE_CODE = re.compile(r"[A-Za-z0-9_-]+")


def is_plausible_code(code: str) -> bool:
    """Whether ``code`` could be a code at all: 1 to ``MAX_CODE_LENGTH`` URL-safe
    characters. Not whether one exists."""
    return len(code) <= MAX_CODE_LENGTH and _PLAUSIBLE_CODE.fullmatch(code) is not None


# --------------------------------------------------------------------------
# Picked handles (owner, 2026-10-15)
# --------------------------------------------------------------------------

HANDLE_MIN_LENGTH = 3
HANDLE_MAX_LENGTH = 30

#: Lowercase letters and digits in runs joined by single hyphens: no leading,
#: trailing or doubled hyphen. The length is checked apart, so the message and
#: the pattern each say one thing.
_HANDLE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

#: Words a handle may not be: the app's own routes and words, and the values a
#: client bug renders as text. The link is ``/invite/{code}``, so a handle that
#: reads as an official page or a broken one is refused.
RESERVED_HANDLES = frozenset(
    {
        "about",
        "account",
        "admin",
        "administrator",
        "api",
        "app",
        "auth",
        "contact",
        "dashboard",
        "edufurther",
        "help",
        "home",
        "invite",
        "invites",
        "login",
        "logout",
        "me",
        "mentor",
        "mentors",
        "nan",
        "null",
        "official",
        "privacy",
        "referral",
        "referrals",
        "root",
        "security",
        "settings",
        "signin",
        "signup",
        "staff",
        "support",
        "system",
        "team",
        "terms",
        "test",
        "undefined",
    }
)

#: How long a retired code stays out of everyone else's reach. Its owner may
#: take it back at any time; after this, anyone may pick it.
RETIRED_CODE_HOLD = timedelta(days=30)


def normalise_handle(raw: str) -> str:
    """Lowercase, before judging: a handle is case-insensitive by being stored
    in one case, so ``Ada`` and ``ada`` can never be two handles."""
    return raw.lower()


def handle_problem(handle: str) -> Literal["malformed", "reserved"] | None:
    """Why ``handle`` may not be picked, or ``None`` when it may.

    Expects the normalised form. Taken and held handles are the store's to
    judge, because only the database knows them.
    """
    if not (HANDLE_MIN_LENGTH <= len(handle) <= HANDLE_MAX_LENGTH and _HANDLE.fullmatch(handle)):
        return "malformed"
    if handle in RESERVED_HANDLES:
        return "reserved"
    return None
