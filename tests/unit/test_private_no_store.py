"""The one `Cache-Control` for a response only its caller may hold.

Every route test compares against `PRIVATE_NO_STORE`, so this is the single
place its value is pinned: `private` keeps it out of shared caches, `no-store`
out of the browser's.
"""

from __future__ import annotations

from app.api.deps import PRIVATE_NO_STORE


def test_no_cache_but_the_callers_may_keep_it() -> None:
    assert PRIVATE_NO_STORE == "private, no-store"
