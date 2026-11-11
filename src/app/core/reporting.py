"""What an unhandled request error is reported with, to Sentry and PostHog
alike (decision 339).

**Built once per 500, by `api/errors.report_server_error`**, which both
`handle_unexpected_error` and `handle_app_error` call, and read by both tools,
so they cannot disagree on the request, the route or the member.

**The member's id is there only with analytics consent**, and only for a
member analytics may name (live, not internal). It is recorded on
the request when the auth dependency resolves the caller (`remember_member`),
from the answer `CURRENT_USER` already read, so the error path never queries
the database that may be what failed.

Pure standard library: `api` writes it, `infra` reads it, and `core` is the
one layer both may import.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID

__all__ = [
    "UNMATCHED_ROUTE",
    "ErrorContext",
    "current_error",
    "member_on",
    "remember_member",
    "reporting",
]

#: The route an error is reported under when no route matched (a 404 path, a
#: mount), in both tools: never the raw path.
UNMATCHED_ROUTE = "/(other)"

#: The attribute on `request.state` holding the reportable member, or ``None``.
_MEMBER = "reportable_member"


@dataclass(frozen=True, slots=True)
class ErrorContext:
    request_id: str | None
    #: The matched route's template (`/sessions/{session_id}`), never the raw
    #: path, whose ids would name records; `/(other)` when nothing matched.
    route: str
    #: `users.id`, only when that member accepted analytics consent.
    member_id: UUID | None


_current: ContextVar[ErrorContext | None] = ContextVar("error_context", default=None)


def remember_member(state: object, user_id: UUID, *, granted: bool | None, sendable: bool) -> None:
    """**The one consent rule for error identity**: the caller's id is kept on
    the request only when they accepted analytics (``granted`` is true) and
    analytics may name them (``sendable``: live and not internal,
    `analytics_scope.sendable`). Declined, unanswered or unsendable keeps
    nothing. ``state`` is the request's `request.state`."""
    setattr(state, _MEMBER, user_id if granted is True and sendable else None)


def member_on(state: object) -> UUID | None:
    """The member `remember_member` kept on this request, if any."""
    member: UUID | None = getattr(state, _MEMBER, None)
    return member


@contextmanager
def reporting(context: ErrorContext) -> Iterator[None]:
    """Make ``context`` the one an error captured inside this block carries."""
    token = _current.set(context)
    try:
        yield
    finally:
        _current.reset(token)


def current_error() -> ErrorContext | None:
    """The context of the error being reported now, or ``None`` outside one."""
    return _current.get()
