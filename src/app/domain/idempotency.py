"""What makes two requests *the same request*.

Pure, and in ``domain`` rather than beside the table, for the reason every rule
here is: the answer to "is this the same booking" is a product decision, not a
storage detail, and it has to be assertable without a database.

**A hash, not the body.** The only question ever asked of a stored request is
*is it the same one*, and keeping the body would be a second copy of user input
with its own retention question — a booking message is something a mentee wrote
to their mentor.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = ["request_fingerprint"]


def request_fingerprint(endpoint: str, body: Any) -> str:
    """A stable hash of *this request*, for comparing against a stored one.

    **The endpoint is part of it.** A client reusing one key across two
    endpoints is a mistake, and folding the endpoint in turns it into a
    mismatch — a 422 that names the problem — rather than a replay that answers
    a booking with somebody's profile update.

    **``sort_keys`` is what makes this a fingerprint rather than a coincidence.**
    JSON object order is not semantic, and two clients serialising the same
    booking in different field orders must not look like different requests —
    which is exactly what a naive hash of the raw bytes would say. That is also
    why this takes the *parsed* body rather than the raw request bytes:
    whitespace and key order are transport, not content.

    **Absent and null are deliberately different.** ``separators`` and the plain
    dump keep ``{"topic": null}`` distinct from ``{}``, because to a PATCH-shaped
    body they mean different things — and a fingerprint that conflated them
    would let a second, genuinely different request replay the first's answer.
    This endpoint is a POST where the two happen to coincide; the rule is here
    because the function is not.
    """
    payload = json.dumps(
        {"endpoint": endpoint, "body": body},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
