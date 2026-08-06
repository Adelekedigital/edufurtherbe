"""Application composition root.

Exempt from the layer check: this is the one place allowed to see everything.
"""

from fastapi import FastAPI

from app.api import errors
from app.api.routes import health, users
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
        "name": "users",
        "description": (
            "The signed-in user's own record. Every endpoint here requires a "
            "Supabase bearer token, and resolves it to a local user through "
            "`users.auth_id` — the token's subject is the vendor's identifier "
            "and is never exposed. "
            "Authorization is profile existence, not a role column: a mentor is "
            "someone with an approved mentor profile, and an admin is someone "
            "holding a live grant. `primary_role` only picks a dashboard."
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
    application.include_router(health.router)
    application.include_router(users.router)
    return application


app = create_app()
