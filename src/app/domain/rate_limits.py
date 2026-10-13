"""At most so many events in any window: a sliding log (decision 242).

Pure: the caller passes the time, so the rule is tested without a clock. A
sliding log rather than a fixed window, so a burst cannot spend two windows'
worth across a boundary; it holds at most `limit` timestamps.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass
class SlidingWindow:
    """At most `limit` events in any `window_seconds`."""

    limit: int
    window_seconds: float
    _events: deque[float] = field(default_factory=deque)
    #: Refusals since the last event was allowed; 1 marks the start of a spell.
    refusals: int = 0

    def allow(self, now: float) -> bool:
        """Record an event at `now` if one is allowed, and say whether it was.
        A refusal is not recorded, so it costs nothing against the window."""
        while self._events and self._events[0] <= now - self.window_seconds:
            self._events.popleft()
        if len(self._events) >= self.limit:
            self.refusals += 1
            return False
        self._events.append(now)
        self.refusals = 0
        return True
