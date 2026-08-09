"""Every failure leaves this service in one shape: RFC 9457 Problem Details.

    {"type": "about:blank", "title": "Not Found", "status": 404, "detail": "..."}

A standard rather than a bespoke envelope, because the Next.js client can handle
one shape uniformly and because the shape that ships first is the one every
client encodes — tier 2 says to decide this before the *second* endpoint exists,
not the tenth.

**Status codes are chosen here and nowhere else.** ``domain`` raises
``AppError`` subclasses that know nothing about HTTP, which is what lets a
domain rule be unit-tested without a request and reused by the ETL, which has no
request at all.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.core.errors import (
    AppError,
    AuthenticationError,
    ConfigurationError,
    ConflictError,
    NotFoundError,
)
from app.core.errors import ValidationError as DomainValidationError

CONTENT_TYPE = "application/problem+json"

# Every deliberate error, mapped once.
#
# `NotFoundError` covers both "does not exist" and "not yours" — the house
# convention conflates them, because distinguishing them tells anyone who can
# enumerate ids which ones are real.
STATUS_BY_ERROR: dict[type[AppError], int] = {
    AuthenticationError: status.HTTP_401_UNAUTHORIZED,
    NotFoundError: status.HTTP_404_NOT_FOUND,
    ConflictError: status.HTTP_409_CONFLICT,
    DomainValidationError: status.HTTP_422_UNPROCESSABLE_CONTENT,
}

# An operator fault, never a caller fault. Mapping a missing setting to a 4xx
# would tell the caller to fix their request when nothing about it is wrong.
OPERATOR_ERROR = status.HTTP_500_INTERNAL_SERVER_ERROR


def problem(
    *, status_code: int, title: str, detail: str | None = None, type_: str = "about:blank"
) -> JSONResponse:
    """Build a Problem Details response."""
    body: dict[str, object] = {"type": type_, "title": title, "status": status_code}
    if detail:
        body["detail"] = detail
    return JSONResponse(status_code=status_code, content=body, media_type=CONTENT_TYPE)


async def handle_app_error(request: Request, exc: Exception) -> JSONResponse:  # noqa: ARG001
    """Map a deliberate domain error to its response.

    ``ConfigurationError`` is handled apart from the table and **its detail is
    withheld**: it names settings, and a message like "SUPABASE_JWKS_URL
    is unset" tells an anonymous caller what this service is wired to.
    """
    # `request` is unused but required: FastAPI calls every exception handler
    # with it, and dropping the parameter changes the signature it dispatches to.
    if isinstance(exc, ConfigurationError):
        return problem(
            status_code=OPERATOR_ERROR,
            title="Internal Server Error",
            detail="The service is misconfigured.",
        )

    for error_type, code in STATUS_BY_ERROR.items():
        if isinstance(exc, error_type):
            # Neither the title nor the detail varies with the cause for a 401:
            # the class name would say as much as a message would.
            if code == status.HTTP_401_UNAUTHORIZED:
                return problem(status_code=code, title="Unauthorized")
            return problem(status_code=code, title=error_type.__name__, detail=str(exc) or None)

    # An `AppError` subclass nobody mapped. 500 rather than a guessed 4xx: an
    # unmapped error is a gap in this table, and reporting it as the caller's
    # fault would hide that.
    return problem(status_code=OPERATOR_ERROR, title="Internal Server Error")


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:  # noqa: ARG001
    """Anything nobody anticipated, still in the one shape.

    **Without this, an unhandled exception leaves as `text/plain`
    "Internal Server Error"** — Starlette's default — which breaks this module's
    whole promise and, worse, breaks it precisely in a client's error path,
    where a JSON parse failure turns one fault into two. Found by pointing the
    application at a database that had not been migrated: the missing table
    raised `ProgrammingError`, and the response was plain text.

    **The detail is withheld, deliberately.** A database error names tables and
    columns, a driver error can name a host, and a third-party client can name a
    URL with a token in it. The same reasoning `ConfigurationError` already
    follows.

    **It is logged with its traceback**, because a 500 that says nothing to the
    caller *and* nothing to the operator is worse than the plain-text one it
    replaces. Stdlib logging rather than structlog: nothing in this application
    configures logging yet, and standing that up is its own piece of work — this
    line at least reaches uvicorn's handler today.
    """
    # `exc` is unused: it is logged through `.exception()`, which reads the active
    # exception, and it must never reach the response body.
    logging.getLogger(__name__).exception(
        "unhandled error serving %s %s", request.method, request.url.path
    )
    return problem(status_code=OPERATOR_ERROR, title="Internal Server Error")


def register(application: FastAPI) -> None:
    """Attach the handlers.

    Order matters: the `AppError` handler is registered first so a deliberate
    error keeps its own status, and the catch-all only sees what nothing else
    claimed. Starlette dispatches on the most specific registered class.
    """
    application.add_exception_handler(AppError, handle_app_error)
    application.add_exception_handler(Exception, handle_unexpected_error)
