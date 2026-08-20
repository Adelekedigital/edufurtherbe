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
    callbacks,
    catalogue,
    health,
    me_calendar,
    me_intake,
    me_session_types,
    mentors,
    session_types,
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
        "name": "public",
        "description": (
            "What a stranger may read about a mentor, with no token at all: "
            "the profile a mentee is choosing from, the offerings they may "
            "book, and the slots those offerings are free in.\n\n"
            "**Two things must both be true for any of it to answer** — the "
            "mentor is `approved` *and* `listed`. The pair is not redundant: "
            "approval and listing are written by separate events so that one "
            "event states one fact, and no constraint ties them, so a `pending` "
            "mentor who is `listed` is a legal row. Gating on listing alone "
            "would publish an unvetted mentor's calendar to anyone who asked."
            "\n\n"
            "Every refusal here is `404`, whatever the reason — no such mentor, "
            "not approved, not listed, offering switched off, offering deleted. "
            "Telling them apart tells anyone who can guess an id which mentors "
            "exist and what state they are in.\n\n"
            "Separate from `catalog`, which is reference data the world owns; "
            "this is data about people who chose to be visible."
        ),
    },
    {
        "name": "availability",
        "description": (
            "What a mentor declares they are free for — **not** what a mentee "
            "can book. Weekly rules and dated exceptions, owned and edited by "
            "the mentor.\n\n"
            "The bookable answer is `GET /users/{id}/availability/slots`, "
            "which takes no token and sits under `public`: it reads these "
            "rules, swaps in the offering's own scheduling windows where it has "
            "any, subtracts exceptions and existing sessions, applies the "
            "notice window, and returns instants. A client computing slots from "
            "the rules directly would be reimplementing that, and would drift "
            "from it.\n\n"
            "Overlapping windows on one day are refused with `409` by an "
            "exclusion constraint rather than by a check before the write — "
            "checking then writing is the race the constraint exists to close."
        ),
    },
    {
        "name": "sessions",
        "description": (
            "A booking, from the moment it is claimed through whatever it "
            "becomes. Reading is scoped to the two parties; writing is booking "
            "and the four transitions.\n\n"
            "**`starts_at` must be an instant `/slots` currently offers**, to "
            "the second. This surface asks that endpoint rather than "
            "reimplementing it, so everything deciding availability applies "
            "here with no second set of rules to disagree with — and every "
            "reason an instant is unavailable is one `422`, because the "
            "client's answer to all of them is to re-read the slots.\n\n"
            "**`Idempotency-Key` is required on booking.** A retry replays the "
            "original response rather than booking a second hour.\n\n"
            "Who may take an action is part of the action: a mentee has no "
            "accept, a mentor has no withdraw, and either party may cancel a "
            "confirmed session. Asking for one that is not yours gets `404` — "
            "the action's URL does not exist for you — where being the right "
            "party in the wrong state gets `409` naming the state."
        ),
    },
    {
        "name": "session-types",
        "description": (
            "A mentor's own offerings and the intake form each one asks — the "
            "management surface, not the shop window.\n\n"
            "**Its own tag rather than `users`**, which it shared until the "
            "surface grew past a single list. Settled decision #64 gives "
            "everything outside the catalogue *its domain name*, and this is a "
            "domain: creating, editing and retiring what a mentor sells, plus "
            "the questions a mentee answers when booking it.\n\n"
            "**Not the same answer as `GET /users/{id}/session-types`**, which "
            "is public and shows only what a mentee may book. An offering you "
            "have switched off is absent there and present here, and while your "
            "profile is unlisted that endpoint answers `404` for you as well as "
            "for everybody else."
        ),
    },
    {
        "name": "callbacks",
        "description": (
            "Endpoints a machine or a redirected browser lands on. **There is "
            "no bearer token on a request here, by design rather than by "
            "mistake**, which is why they are grouped apart: one of these "
            "sitting among the authenticated routes is how somebody later "
            "assumes a caller is present.\n\n"
            "Each carries its own proof instead. A scheduler signs its "
            "callbacks; an OAuth redirect carries a sealed, short-lived "
            "`state` naming the user who started the flow. Neither is a "
            "credential the caller chose, and both are refused when absent.\n\n"
            "Every callback re-reads the state it is about before acting, so "
            "one that arrives for something already settled does nothing and "
            "says so. That is what lets work be scheduled ahead without "
            "anything ever having to cancel it, and it is what makes these "
            "safe to call twice — the schedulers that drive them retry by "
            "design."
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
    # **The app carries its own settings**, the way it already carries its
    # session factory and token verifier. Without this a dependency calling
    # `get_settings()` reads the process-wide cache, so an app built for a test
    # — or a second app in one process — silently runs on somebody else's
    # configuration. Found by a callback suite whose signing key was configured
    # on the app and ignored by the verifier.
    application.state.settings = resolved

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
    application.include_router(mentors.router)
    application.include_router(user_attributes.router)
    application.include_router(availability.router)
    application.include_router(slots.router)
    application.include_router(session_types.router)
    application.include_router(me_session_types.router)
    application.include_router(me_calendar.router)
    application.include_router(me_calendar.callback_router)
    application.include_router(me_intake.router)
    application.include_router(callbacks.router)
    application.include_router(sessions.router)
    application.include_router(admin.router)
    return application


app = create_app()
