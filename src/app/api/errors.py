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
    withheld**: it names settings, and a message like "EDUFURTHER_SUPABASE_JWKS_URL
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


def register(application: FastAPI) -> None:
    """Attach the handler for every deliberate error."""
    application.add_exception_handler(AppError, handle_app_error)
