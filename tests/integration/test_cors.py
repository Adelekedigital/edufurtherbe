"""Whether the browser actually gets a CORS header, asserted through a response.

Read through responses rather than by inspecting ``app.user_middleware``: the
question is what a browser is told, and a middleware present but misconfigured
would satisfy an object check while denying every request.

The absent case is the one worth the most here. A CORS layer configured with no
origins allows nothing, which looks identical to no layer at all until the day
somebody adds an origin and cannot work out why it is ignored.
"""

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

ALLOWED = "https://app.edufurther.org"
STRANGER = "https://not-ours.example"
ALLOW_ORIGIN = "access-control-allow-origin"
ALLOW_CREDENTIALS = "access-control-allow-credentials"


def client(*origins: str) -> TestClient:
    return TestClient(
        create_app(Settings(_env_file=None, environment="ci", cors_origins=list(origins)))
    )


def test_a_configured_origin_is_allowed() -> None:
    response = client(ALLOWED).get("/health", headers={"Origin": ALLOWED})

    assert response.status_code == 200
    assert response.headers.get(ALLOW_ORIGIN) == ALLOWED


def test_an_unconfigured_origin_is_not_allowed() -> None:
    """The header is absent, not set to the caller's origin.

    Echoing back whatever arrives is the classic misconfiguration, and it passes
    any test that only checks the request succeeded.
    """
    response = client(ALLOWED).get("/health", headers={"Origin": STRANGER})

    assert response.status_code == 200
    assert response.headers.get(ALLOW_ORIGIN) is None


def test_no_origins_configured_means_no_cors_headers() -> None:
    """The default, and the state that unblocked the broken deploy."""
    response = client().get("/health", headers={"Origin": ALLOWED})

    assert response.status_code == 200
    assert response.headers.get(ALLOW_ORIGIN) is None


def test_credentials_are_allowed_for_a_configured_origin() -> None:
    """The frontend sends an ``Authorization`` header, so this has to be on.

    Starlette refuses to combine credentials with a wildcard, so this passing is
    also evidence that none of the three lists is ``*``.
    """
    response = client(ALLOWED).get("/health", headers={"Origin": ALLOWED})

    assert response.headers.get(ALLOW_CREDENTIALS) == "true"


def test_a_preflight_for_a_real_call_is_answered() -> None:
    """``OPTIONS`` with the headers a browser actually sends before a POST."""
    response = client(ALLOWED).options(
        "/api/v1/me",
        headers={
            "Origin": ALLOWED,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers.get(ALLOW_ORIGIN) == ALLOWED
    allowed_headers = response.headers.get("access-control-allow-headers", "").lower()
    assert "authorization" in allowed_headers


def test_a_preflight_from_a_stranger_is_not_answered_with_permission() -> None:
    response = client(ALLOWED).options(
        "/api/v1/me",
        headers={"Origin": STRANGER, "Access-Control-Request-Method": "POST"},
    )

    assert response.headers.get(ALLOW_ORIGIN) is None


def test_a_refused_body_still_carries_its_cors_header() -> None:
    """A 413 from the body-limit middleware, seen by a browser.

    The limit is registered *before* CORS, which puts CORS outside it — Starlette
    wraps in reverse. Get that order wrong and the refusal leaves without an
    `Access-Control-Allow-Origin`, so the browser reports an opaque network
    failure and the user is told nothing about a file being too large.

    Asserted through a response for the same reason as every other test here: a
    middleware present but ordered wrongly satisfies an object check.
    """
    from app.api.limits import MAX_BODY_BYTES

    response = client(ALLOWED).post(
        "/api/v1/users/00000000-0000-0000-0000-000000000000/avatar",
        content=b"\x00" * (MAX_BODY_BYTES + 1),
        headers={"Origin": ALLOWED, "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 413, response.text
    assert response.headers.get(ALLOW_ORIGIN) == ALLOWED
    assert response.headers.get(ALLOW_CREDENTIALS) == "true"


def test_the_refusal_is_problem_json_like_every_other_error() -> None:
    """One error shape, including the one written by hand in the middleware.

    `limits.py` cannot use `errors.problem()` — it answers below the application,
    where no exception handler runs — so this is the only thing keeping the two
    spellings of a Problem Details body in step.
    """
    import json

    from app.api.errors import CONTENT_TYPE, problem
    from app.api.limits import MAX_BODY_BYTES

    refused = client(ALLOWED).post(
        "/api/v1/users/00000000-0000-0000-0000-000000000000/avatar",
        content=b"\x00" * (MAX_BODY_BYTES + 1),
        headers={"Content-Type": "application/octet-stream"},
    )
    # Compared against `problem()` itself rather than against another endpoint:
    # a 401 omits `detail` deliberately, so any endpoint chosen as the reference
    # brings its own omissions and the comparison stops being about the shape.
    generated = json.loads(problem(status_code=413, title="Content Too Large", detail="x").body)

    assert refused.headers["content-type"].startswith(CONTENT_TYPE)
    assert set(refused.json()) == set(generated), (
        "the hand-written problem body has different keys from the generated one"
    )
    assert refused.json()["status"] == 413
