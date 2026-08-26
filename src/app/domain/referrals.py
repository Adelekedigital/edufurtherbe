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

import secrets

__all__ = ["CODE_LENGTH", "make_code", "may_qualify"]

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
