"""Fetching a legacy asset from wherever Bubble left it.

**Never `HEAD`.** Measured against the real export: ``app.edufurther.org`` answers
`HEAD` with **403** and the identical `GET` with **200**, for twelve of the
twenty-one dev assets. An exists-check or size-probe built on `HEAD` would have
reported most of the collection missing — and would have looked like a Bubble
outage rather than a wrong verb.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from app.infra.http.retry import send_with_backoff


@dataclass(frozen=True, slots=True)
class Fetched:
    """The bytes, and nothing the server claimed about them.

    No content type: the source's ``Content-Type`` is not trusted, because the
    filenames are not either. ``domain.assets.sniff_content_type`` decides.
    """

    payload: bytes


class AssetSource:
    """Reads a legacy asset URL. Anonymous — these files are publicly served."""

    def __init__(
        self,
        client: httpx.Client,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._sleep = sleep

    def fetch(self, url: str) -> Fetched | None:
        """The bytes, or ``None`` when the source will not serve them.

        ``None`` rather than an exception, because "Bubble will not give us this
        file" is an expected outcome with a known instance: one dev asset answers
        401 with a zero-length body. That user keeps a null image; it is not a
        reason to stop the other 1,199.

        A network error *is* raised — that is a condition worth retrying the run
        for, and it should not be silently recorded as "the user had no image".
        """
        response = send_with_backoff(
            lambda: self._client.get(url, follow_redirects=True), self._sleep
        )
        if response.status_code >= httpx.codes.BAD_REQUEST:
            return None
        if not response.content:
            # A 200 with an empty body is not an image. Treated as absent rather
            # than uploaded, so nothing publishes a zero-byte object.
            return None
        return Fetched(payload=response.content)
