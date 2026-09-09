"""What a third party said when it refused.

``httpx`` renders only the status line, so a service that explains itself in the
response body explains itself to nobody. QStash answers a wrong region with
``404`` and names the region in the body: without this the operator sees "404 Not
Found" against a URL spelled exactly right and goes looking for a broken path,
which is the one place the answer is not. That cost an afternoon.

The schedule reconciler and the callback publisher are the two callers, which is
the extract-on-the-second-occurrence case non-negotiable #8 names — and the
second copy was written in the same change as the first, which is how a rule
about drift starts drifting immediately.

**Operator-facing only, and that is load-bearing.** ``UpstreamError`` maps to a
``502`` whose ``detail`` is ``str(exc)``, so anything returned here would be
served verbatim in a problem document. That is unreachable today: the reconciler
is built only by its CLI, and ``SchedulerError`` is caught and logged at both of
its call sites. **Put either behind an endpoint and this becomes a response
body** — a third party's error text, unreviewed, returned to a caller. Summarise
here before that happens rather than after.
"""

from __future__ import annotations

import httpx

#: Enough for a service to finish a sentence, short enough that a page of HTML
#: from a proxy does not become the whole log line.
LIMIT = 400


def why(exc: Exception, response: httpx.Response | None = None) -> str:
    """The failure, with the upstream's own explanation when it gave one.

    Safe for any exception: an ``httpx.RequestError`` carries no response at all,
    and a decode error carries one whose body is the thing that failed to parse.
    Both fall back to the exception's own text.

    **``response`` is accepted explicitly, and not only read off ``exc``.** A
    ``json.JSONDecodeError`` is a plain ``ValueError`` raised by the parser, not
    by ``httpx`` — it carries no ``.response`` at all, so a caller that already
    holds the response it tried to parse passes it here rather than losing the
    one piece of information worth logging.

    A streamed response that has not been read raises ``httpx.ResponseNotRead``
    on ``.text`` itself — a second failure from inside this function's own error
    handling. Caught rather than propagated, for the same reason the body is
    summarised at all: this runs while explaining a different failure, and is
    not the place to raise a new one.
    """
    response = response if response is not None else getattr(exc, "response", None)
    if response is None:
        return f"{exc}"
    try:
        body = response.text.strip()
    except httpx.StreamError:
        return f"{exc}"
    return f"{exc}; upstream said: {body[:LIMIT]}" if body else f"{exc}"


def trim_origin(url: str) -> str:
    """Drop a trailing slash so a caller's own path does not double up.

    A pasted console URL carries one, and ``https://host/`` + ``/v2/publish`` is
    a doubled slash — a ``404`` wearing the same face as the wrong-region ``404``
    this module exists to explain. The scheduler and the reconciler both trim
    their configured origin at construction; one function rather than two
    inline copies, for the same reason ``why`` is.
    """
    return url.rstrip("/")
