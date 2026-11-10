"""What the product needs from outside, named without naming who provides it.

A Protocol here states the need; an adapter in `infra` satisfies it and a
factory there chooses the real one or the null one from settings (decision
239). Synchronous, like every adapter: async callers reach them through
`asyncio.to_thread`.
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

__all__ = ["AnalyticsSink"]


class AnalyticsSink(Protocol):
    """Where product analytics events go (PRD 30)."""

    def capture(self, events: list[dict[str, Any]]) -> None:
        """Send these events, built by `analytics_event`, in one request.
        Raises `UpstreamError` when they were not taken."""
        ...

    def delete_person(self, user_id: UUID) -> None:
        """Erase the person ``user_id`` names, and their events, at the vendor
        (the account purge). Raises `UpstreamError` when it was not taken."""
        ...
