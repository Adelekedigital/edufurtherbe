"""What a template says it needs, asked of the provider that publishes it.

**A port with two adapters**, following `_rooms` and `_calendar`. That buys three
things: tests need no network, a static set is a working fallback when Loops is
slow, and a deployment can start static and switch without a code change.

**The provider is the source of truth for what a message contains.** That is the
inversion this design accepts deliberately: whoever edits a template controls
what it requires, which is the deploy-free flexibility, and the cost is that a
template edit can break sends. It breaks *loudly* — see
:func:`app.domain.messages.build_variables` — which is the acceptable version of
that cost.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

import httpx

from app.core.errors import UpstreamError

__all__ = [
    "LOOPS_TRANSACTIONAL_API",
    "PER_PAGE",
    "LoopsTemplates",
    "StaticTemplates",
    "TemplateVariables",
]

logger = logging.getLogger(__name__)

LOOPS_TRANSACTIONAL_API = "https://app.loops.so/api/v1/transactional-emails"

#: The provider's maximum. Asking for more is refused rather than clamped, and
#: asking for fewer only means more round trips to learn the same thing.
PER_PAGE = 50

TIMEOUT = httpx.Timeout(15.0)


class TemplateVariables(Protocol):
    """What a template declares, by its provider-side id."""

    def declared(self, template_id: str) -> frozenset[str]: ...


class StaticTemplates:
    """A fixed map. The fallback, and what tests use.

    **Not a stub for something missing.** A deployment that would rather pin the
    variable sets than depend on the provider at send time can wire this and get
    exactly the same behaviour, minus the ability to notice a template changing.
    """

    def __init__(self, declared: dict[str, frozenset[str]] | None = None) -> None:
        self._declared = declared or {}

    def declared(self, template_id: str) -> frozenset[str]:
        return self._declared.get(template_id, frozenset())


class LoopsTemplates:
    """Loops, which publishes each template's merge fields.

    **Two endpoints, both used.** The list primes the whole cache in one call;
    the single-template endpoint answers a miss. That pairing is not
    belt-and-braces — the likeliest cause of an id the cache has never seen is a
    template published seconds ago, and fetching *that one* is the precise
    answer where refetching all of them to learn one thing is the crude one.

    **The list is cursor-paginated and `perPage` caps at 50.** An implementation
    that reads the first page and stops works perfectly on nine templates and
    silently drops them once an account passes fifty — a bug that ships green,
    which is why the paging is here rather than left for later.

    **Empty means unpublished, not "needs nothing".** Loops reports no variables
    for a draft. This returns what the provider said and lets the caller refuse;
    quietly treating it as an empty requirement would send a blank email, which
    is the failure the whole design exists to prevent.
    """

    def __init__(self, api_key: str, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(
            headers={"Authorization": f"Bearer {api_key}"}, timeout=TIMEOUT
        )
        self._cache: dict[str, frozenset[str]] = {}
        self._primed = False

    def declared(self, template_id: str) -> frozenset[str]:
        """This template's merge fields, from cache where possible.

        A miss costs one request for one template. A cold cache costs one
        request per fifty templates, once.
        """
        if not self._primed:
            self.prime()
        if template_id not in self._cache:
            self._cache[template_id] = self._fetch_one(template_id)
        return self._cache[template_id]

    def prime(self) -> None:
        """Read every template, following the cursor to the end.

        Failure is logged and not raised: a cold cache falls through to
        per-template fetches, which is slower and still correct, where raising
        would stop a sweep that has other messages to send.
        """
        try:
            for template in self._pages():
                self._cache[str(template["id"])] = frozenset(
                    str(name) for name in template.get("dataVariables") or ()
                )
        except UpstreamError as exc:
            logger.warning("could not read the template list: %s", exc)
        self._primed = True

    def _pages(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, str] = {"perPage": str(PER_PAGE)}
            if cursor:
                params["cursor"] = cursor
            payload = self._call(LOOPS_TRANSACTIONAL_API, params)
            found.extend(payload.get("data") or [])
            # **The cursor, not a page count.** A `nextCursor` of null is the
            # only thing that means "no more"; a short page does not.
            cursor = (payload.get("pagination") or {}).get("nextCursor")
            if not cursor:
                return found

    def _fetch_one(self, template_id: str) -> frozenset[str]:
        payload = self._call(f"{LOOPS_TRANSACTIONAL_API}/{template_id}", None)
        return frozenset(str(name) for name in payload.get("dataVariables") or ())

    def _call(self, url: str, params: dict[str, str] | None) -> dict[str, Any]:
        """One place where a Loops failure becomes ours, as `DailyRooms` does."""
        try:
            response = self._client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise UpstreamError(f"loops template lookup failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise UpstreamError(f"loops returned {type(payload).__name__} for a template")
        return payload
