"""The one HTTP client per timeout this process sends outbound requests through.

Every adapter used to take `client or httpx.Client(...)`, and the client it built
had no owner: nothing closed it, so each booking, accept and email left a
connection pool behind for the garbage collector to find (#370). A client is
meant to be long-lived and shared, which is also what httpx advises, so an
adapter given none borrows this one. Credentials travel on each request, never on
the shared client, where they would be every adapter's.

Thread-safe: adapters are called through `asyncio.to_thread`, so two threads can
ask for the first client at once. `httpx.Client` itself is safe to share across
threads.
"""

from __future__ import annotations

import threading
from http.cookiejar import DefaultCookiePolicy

import httpx

_clients: dict[str, httpx.Client] = {}
_lock = threading.Lock()


def shared(timeout: httpx.Timeout) -> httpx.Client:
    """The process's client for requests with this timeout, made on first use
    and kept for the life of the process."""
    key = repr(timeout)
    with _lock:
        found = _clients.get(key)
        if found is None or found.is_closed:
            found = _clients[key] = httpx.Client(timeout=timeout)
            # **No cookies kept.** This one client serves every user's calls, so
            # a cookie a provider set on one mentor's request would ride along on
            # the next mentor's. Nothing here authenticates by cookie.
            found.cookies.jar.set_policy(DefaultCookiePolicy(allowed_domains=[]))
        return found
