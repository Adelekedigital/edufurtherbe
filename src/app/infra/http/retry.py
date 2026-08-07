"""Retrying a rate-limited request, once, for every outbound client.

This began inside ``infra/auth/admin.py``. The asset uploader is the second
caller, which is the extract-on-the-second-occurrence case non-negotiable #8
names — and a retry policy is exactly the kind of thing that gets copied and then
diverges silently, because both copies keep working and only one gets the fix.

Nothing here is Supabase-specific: 429 with an optional ``Retry-After`` is
ordinary HTTP.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

# A migration run makes thousands of calls, so a 429 is an expected event
# mid-cutover rather than an exceptional one, and treating it as a failure would
# abandon work the next attempt has to find again. Five attempts with doubling
# backoff spans ~15s.
MAX_ATTEMPTS = 5
BACKOFF_SECONDS = 1.0
# A misconfigured proxy can answer `Retry-After: 3600`. Waiting an hour inside a
# freeze is worse than failing the item and letting the operator re-run.
MAX_BACKOFF_SECONDS = 30.0


def backoff_for(response: httpx.Response, attempt: int) -> float:
    """The server's ``Retry-After`` when it gives one, doubling otherwise."""
    header: str = response.headers.get("Retry-After", "")
    try:
        # `max(0.0, ...)`: a negative value would reach `time.sleep`, which
        # raises — recorded by the caller as a failure for an item that only
        # needed to wait.
        return max(0.0, min(float(header), MAX_BACKOFF_SECONDS))
    except ValueError:
        # Absent, or an HTTP-date rather than seconds. Either way, fall back
        # rather than fail — the point is to wait, not to parse.
        return min(BACKOFF_SECONDS * 2.0 ** (attempt - 1), MAX_BACKOFF_SECONDS)


def send_with_backoff(
    request: Callable[[], httpx.Response],
    sleep: Callable[[float], None] = time.sleep,
) -> httpx.Response:
    """Perform a request, retrying **only** a 429.

    Deliberately narrow. A 5xx is not retried: a timed-out write may well have
    succeeded, and a blind retry would either duplicate it or lean on a
    de-duplication branch to clean up after itself. Re-running the whole command
    is the safe recovery, because every migration action here is idempotent by
    design.

    ``sleep`` is injected so a test can assert the backoff without spending it.
    """
    attempt = 0
    while True:
        response = request()
        attempt += 1
        if response.status_code != httpx.codes.TOO_MANY_REQUESTS or attempt >= MAX_ATTEMPTS:
            return response
        sleep(backoff_for(response, attempt))
