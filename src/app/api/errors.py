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
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.errors import (
    AccountExistsError,
    AlreadyReviewedError,
    AppError,
    AuthenticationError,
    ConfigurationError,
    ConflictError,
    InsufficientCreditError,
    NotFoundError,
    OnboardingIncompleteError,
    ReviewIntervalError,
    SessionTypeHasBookingsError,
    UpstreamError,
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
    # **502, not 503.** The fault is always upstream — a provider refused, was
    # unreachable, or answered in a shape we cannot use. 503 would claim *this*
    # service is unavailable and invite a retry against a request that will fail
    # the same way.
    UpstreamError: status.HTTP_502_BAD_GATEWAY,
}

# The `type` slot, used for the first time here.
#
# **Settled decision #110 left this open and named the condition.** It shipped a
# `409` with no machine-readable reason because there was exactly one refusal to
# distinguish, and recorded: *"A second refusal exists. Then the 409 needs a
# machine-readable reason and the `type` slot in the RFC 9457 envelope is where
# it goes — additive, so nothing shipped here blocks it."* `POST /reviews` is
# that second refusal, twice over: one terminal, one that resolves with time,
# and a client that cannot tell them apart either retries forever or gives up
# on a review it could have written next month.
#
# **Relative URIs, resolved against the API root.** RFC 9457 permits them and
# they stay correct across environments, where an absolute URL would either
# name a host that differs per deployment or a documentation site that has to
# exist. The strings are a published contract: a client branches on them, so
# they are stable in the way a status code is.
#
# **Mapped here rather than raised with the error**, because this module is
# where transport concerns live — `core/errors.py` names conditions and knows
# nothing about problem documents, the same split that keeps `domain/` testable
# without a request.
TYPE_BY_ERROR: dict[type[AppError], str] = {
    AccountExistsError: "/problems/account-exists",
    InsufficientCreditError: "/problems/insufficient-credit",
    OnboardingIncompleteError: "/problems/onboarding-incomplete",
    AlreadyReviewedError: "/problems/review-already-exists",
    ReviewIntervalError: "/problems/review-interval-not-elapsed",
    SessionTypeHasBookingsError: "/problems/session-type-has-bookings",
}

# An operator fault, never a caller fault. Mapping a missing setting to a 4xx
# would tell the caller to fix their request when nothing about it is wrong.
OPERATOR_ERROR = status.HTTP_500_INTERNAL_SERVER_ERROR


def problem(
    *,
    status_code: int,
    title: str,
    detail: str | None = None,
    type_: str = "about:blank",
    errors: list[dict[str, str]] | None = None,
    members: dict[str, object] | None = None,
) -> JSONResponse:
    """Build a Problem Details response.

    `errors` is the RFC 9457 extension for field-level problems, sent on every
    422 and on nothing else (settled decision #195).
    """
    body: dict[str, object] = {"type": type_, "title": title, "status": status_code}
    if detail:
        body["detail"] = detail
    if errors is not None:
        body["errors"] = errors
    # Extension members a problem type carries (#197), never overriding the
    # standard ones.
    for name, value in (members or {}).items():
        body.setdefault(name, value)
    return JSONResponse(status_code=status_code, content=body, media_type=CONTENT_TYPE)


#: Where a parameter that is not the body lives. Its pointer is the location
#: then the name, `/query/limit`, so a body field and a parameter never share
#: a pointer — the body's own fields start at the root.
PARAMETER_LOCATIONS = frozenset({"query", "path", "header", "cookie"})


def json_pointer(tokens: tuple[object, ...] | list[object]) -> str:
    """An RFC 6901 JSON Pointer: each token escaped, `~` as `~0` then `/` as `~1`."""
    return "".join("/" + str(t).replace("~", "~0").replace("/", "~1") for t in tokens)


def pointer(loc: tuple[object, ...] | list[object]) -> str:
    """Where a validation error is, as a client can resolve it.

    **Into the body for a body field** — `/answers/0/text` is the JSON Pointer a
    client applies to what it sent. **Location then name for a parameter** —
    `/query/limit`, `/path/user_id` — because a parameter is not in the body.
    """
    if not loc:
        return ""
    where, *rest = loc
    if where in PARAMETER_LOCATIONS:
        return json_pointer([where, *rest])
    return json_pointer(rest)


def _problem_type(exc: Exception) -> str:
    """The problem type for a refusal, or the default when it has none.

    Most errors have none and should not: a type is a promise that a client may
    branch on the value, and inventing one per error would publish a vocabulary
    nobody maintains. Only a refusal a caller must respond to *differently* earns
    one.
    """
    for error_type, uri in TYPE_BY_ERROR.items():
        if isinstance(exc, error_type):
            return uri
    return "about:blank"


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
            # **The exception's own name, not the key it matched.** They are the
            # same for every error with no subclass, and different for exactly the
            # two the `type` slot exists to tell apart — which would otherwise
            # both render `ConflictError` in the one field a human reads.
            return problem(
                status_code=code,
                title=type(exc).__name__,
                detail=str(exc) or None,
                type_=_problem_type(exc),
                # **Every 422 carries `errors`**, empty when the refusal is about
                # the request as a whole: one shape, so a client reads the list
                # without first asking whether it is there.
                errors=[] if code == status.HTTP_422_UNPROCESSABLE_CONTENT else None,
                members=exc.problem_members() if isinstance(exc, AppError) else None,
            )

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


async def handle_request_validation_error(
    request: Request,  # noqa: ARG001
    exc: Exception,
) -> JSONResponse:
    """A malformed request body, in the one shape.

    **FastAPI answers `RequestValidationError` itself, with `{"detail": [...]}`.**
    That is not Problem Details, so without this every 422 from body validation
    breaks the promise this module exists to keep — and it breaks it on the most
    ordinary failure there is, a form with a bad field.

    Nothing exposed it until writes arrived: the only 422 before this PR came
    from our own `ValidationError`, raised for a forged cursor.

    The field errors **are** returned, unlike a 500's detail. They describe the
    caller's own request, tell them nothing about this service, and without them
    a client cannot say which field was wrong.
    """
    errors: list[dict[str, Any]] = getattr(exc, "errors", lambda: [])()
    detail = "; ".join(
        f"{'.'.join(str(part) for part in error.get('loc', ())[1:])}: {error.get('msg', '')}"
        for error in errors
    )
    return problem(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        title="Unprocessable Content",
        detail=detail or None,
        # **The message only, never `input`.** Pydantic's error carries the
        # submitted value; a password typed into the wrong field must not come
        # back in the response, or land in a log that records response bodies.
        errors=[
            {"pointer": pointer(error.get("loc", ())), "message": str(error.get("msg", ""))}
            for error in errors
        ],
    )


#: The published shape of every 422, replacing FastAPI's `{"detail": [...]}`,
#: which this service has never sent. `components.schemas` keeps FastAPI's two
#: names so every route that already refers to them now documents the truth.
VALIDATION_ERROR_SCHEMA: dict[str, Any] = {
    "title": "ValidationError",
    "type": "object",
    "required": ["pointer", "message"],
    "properties": {
        "pointer": {
            "type": "string",
            "title": "Pointer",
            "description": (
                "RFC 6901 JSON Pointer. Into the request body for a body field "
                "(`/answers/0/text`); `/<location>/<name>` for a parameter "
                "(`/query/limit`, `/path/user_id`). Empty for the body as a whole."
            ),
        },
        "message": {
            "type": "string",
            "title": "Message",
            "description": "What is wrong. Never echoes the submitted value.",
        },
    },
}
PROBLEM_422_SCHEMA: dict[str, Any] = {
    "title": "HTTPValidationError",
    "type": "object",
    "description": (
        "RFC 9457 Problem Details, as every error from this API. A 422 always "
        "carries `errors` — one entry per invalid field, or empty when the "
        "request is refused as a whole — beside the human-readable `detail`."
    ),
    "required": ["type", "title", "status", "errors"],
    "properties": {
        "type": {"type": "string", "title": "Type"},
        "title": {"type": "string", "title": "Title"},
        "status": {"type": "integer", "title": "Status"},
        "detail": {"type": "string", "title": "Detail"},
        "errors": {
            "type": "array",
            "title": "Errors",
            "items": {"$ref": "#/components/schemas/ValidationError"},
        },
    },
}


def document_problem_422(application: FastAPI) -> None:
    """Publish the real 422 shape in the OpenAPI document.

    FastAPI documents every route's 422 as `HTTPValidationError` —
    `{"detail": [{"loc", "msg", "type"}]}` — which is what *its* handler sends
    and ours replaces. The route declarations already point at those two
    component names, so rewriting the components corrects every route at once.
    """
    generate = application.openapi

    def openapi() -> dict[str, Any]:
        if application.openapi_schema:
            return application.openapi_schema
        schema = generate()
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        components["ValidationError"] = VALIDATION_ERROR_SCHEMA
        components["HTTPValidationError"] = PROBLEM_422_SCHEMA
        return schema

    application.openapi = openapi  # type: ignore[method-assign]


def register(application: FastAPI) -> None:
    """Attach the handlers.

    Order matters: the `AppError` handler is registered first so a deliberate
    error keeps its own status, and the catch-all only sees what nothing else
    claimed. Starlette dispatches on the most specific registered class.
    """
    application.add_exception_handler(AppError, handle_app_error)
    # FastAPI installs its own handler for this one, so ours has to replace it
    # explicitly — the catch-all below never sees it.
    application.add_exception_handler(RequestValidationError, handle_request_validation_error)
    application.add_exception_handler(Exception, handle_unexpected_error)
