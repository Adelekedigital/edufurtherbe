"""Minting an access token locally, for development and for the test suite.

Lives beside the verifier because the two are one contract. The audience, the
algorithm and the subject claim have to agree, and a minter that drifts from the
verifier produces tokens that fail for a reason nobody can read from either side.

**Why a token minter ships in `src/` at all, and why it is not a hole, is
settled decision #96.** Not restated here — one fact, one home (#51).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt

from app.infra.auth.supabase import AUDIENCE, SYMMETRIC_ALGORITHM

__all__ = ["DEFAULT_TTL", "mint_dev_token"]

#: Short by default. A development token is convenience, and one that outlives
#: the afternoon it was minted for is a credential somebody keeps in a shell
#: history.
DEFAULT_TTL = timedelta(hours=1)


def mint_dev_token(
    subject: str | UUID,
    *,
    secret: str,
    ttl: timedelta = DEFAULT_TTL,
    email: str = "someone@example.com",
    **overrides: Any,
) -> str:
    """A signed access token for ``subject``.

    ``overrides`` replace individual claims after the set is built. That is what
    lets a caller mint a token which is expired, or addressed to another
    audience, without keeping a second copy of the claim set in step — the
    rejection tests need exactly those shapes, and they are the tests that
    matter here.
    """
    claims: dict[str, Any] = {
        "sub": str(subject),
        "aud": AUDIENCE,
        "exp": datetime.now(UTC) + ttl,
        "email": email,
    }
    return jwt.encode(claims | overrides, secret, algorithm=SYMMETRIC_ALGORITHM)
