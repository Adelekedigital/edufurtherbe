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
