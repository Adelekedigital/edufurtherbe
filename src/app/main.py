"""Application composition root.

Exempt from the layer check: this is the one place allowed to see everything.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import errors
from app.api.limits import BodyLimitMiddleware
from app.api.routes import (
    admin,
    availability,
    catalogue,
    health,
    sessions,
    slots,
    user_attributes,
    users,
)
from app.core.config import Settings, get_settings

# Tag metadata, so /docs explains each group rather than listing bare paths.
# A reader arriving at the schema cold should learn what a group is for and what
# it deliberately is not, without opening a route module.
OPENAPI_TAGS: list[dict[str, str]] = [
    {
        "name": "health",
        "description": (
            "Liveness. Deliberately touches no dependency — a health check that "
            "fails when the database is slow causes a restart of a process that "
            "was fine. Readiness is a separate concern, added when there is a "
            "dependency worth reporting on."
        ),
    },
    {
        "name": "admin",
        "description": (
            "The review surface. **The only endpoints that show one user another "
            "user's records by design** — the control is the caller's grant "
            "rather than a row scope.\n\n"
            "A caller without a grant receives 404, never 403: a 403 would "
            "confirm the endpoint exists and that somebody may use it.\n\n"
            "Grants are role-specific. `mentor_approval` decides applications "
            "and does not curate the catalogue; `limited_access` may read both "
            "queues and change nothing; `super_admin` may do everything."
        ),
    },
    {
        "name": "catalog",
        "description": (
            "Public reference data, readable without a token: the mirrored "
            "university catalogue (ADR 0020) and the lookup lists a profile form "
            "is built from. Unauthenticated because school selection happens "
            "during signup, before an account exists.\n\n"
            "**Reading is public; adding is not.** A school the catalogue does "
            "not hold is created on the authenticated education write, in the "
            "same transaction, with `created_by` set to the caller.\n\n"
            "Results exclude entries awaiting review and ones merged into "
            "another, so an institution somebody typed yesterday is not offered "
            "to everybody else until an admin has seen it."
        ),
    },
    {
        "name": "users",
        "description": (
            "A user's own record and attributes — and, for a platform admin, "
            "another user's. Every endpoint here requires a "
            "Supabase bearer token, and resolves it to a local user through "
            "`users.auth_id` — the token's subject is the vendor's identifier "
            "and is never exposed. "
            "Authorization is profile existence, not a role column: a mentor is "
            "someone with an approved mentor profile, and an admin is someone "
            "holding a live grant. `primary_role` only picks a dashboard.\n\n"
            "Reading another user's records requires a **live** admin grant; "
            "anyone else receives 404, indistinguishable from a user that does "
            "not exist."
        ),
    },
]


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    Takes settings as an argument so tests construct an app without touching the
    process environment or the cached ``get_settings`` singleton.
    """
    resolved = settings or get_settings()
    application = FastAPI(
        title="EduFurther API",
        version="0.1.0",
        debug=resolved.debug,
        openapi_tags=OPENAPI_TAGS,
    )
    # Registered before the routers so every failure below leaves in the same
    # shape — RFC 9457 Problem Details, never FastAPI's own `{"detail": ...}`.
    errors.register(application)

    # A ceiling on the request body, by declared length and then by counting the
    # bytes that arrive. It has to be middleware: FastAPI parses the multipart
    # form to resolve an `UploadFile`, spooling the whole body to disk, and only
    # then runs the endpoint — so a check written there limits nothing.
    #
    # Registered before CORS, which puts CORS *outside* it (Starlette wraps in
    # reverse). That is the order we want: a 413 still leaves with its CORS
    # headers, so a browser sees the refusal instead of an opaque network error.
    application.add_middleware(BodyLimitMiddleware)

    # Added only when origins are configured. An empty list would install a
    # middleware that allows nothing, which is indistinguishable from no CORS at
    # all until somebody adds an origin and cannot work out why it is ignored.
    #
    # Every list is explicit, and that is a requirement rather than a style: with
    # `allow_credentials=True`, Starlette refuses `*` for origins, methods *and*
    # headers. The frontend sends an `Authorization` header (ADR 0009), so
    # credentials are on and the wildcards are therefore unavailable.
    if resolved.cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=resolved.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            # `Accept`, `Accept-Language`, `Content-Language` and `Content-Type`
            # are always allowed by the middleware; `Authorization` is not, and
            # is the one this API cannot work without.
            allow_headers=["Authorization", "Content-Type"],
        )
    application.include_router(health.router)
    application.include_router(users.router)
    application.include_router(catalogue.router)
    application.include_router(user_attributes.router)
    application.include_router(availability.router)
    application.include_router(slots.router)
    application.include_router(sessions.router)
    application.include_router(admin.router)
    return application


app = create_app()
