"""Mentor profile link tracking: what a visit or a share records (decision 346)."""

from __future__ import annotations

#: How long a visitor key is: one HMAC-SHA256 digest.
VISITOR_KEY_BYTES = 32
#: The longest host name DNS allows; a longer referrer is dropped, not cut.
MAX_REFERRER_HOST = 253
