"""The consent request, the code exchange, and the seal that carries the mentor.

**These assert what is *sent*, not what comes back.** Every defect this file
exists to catch is in an outgoing request: a consent that forgets
``prompt=consent`` returns a working access token and a connection that dies in
an hour, and a redirect that disagrees with the one sent at consent time fails
only in production, only for the first mentor to try it. A test that stubbed the
response and checked the return value would pass through all of it.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet

from app.core.errors import ConfigurationError
from app.infra.clients.meetings import (
    FREEBUSY_SCOPE,
    GOOGLE_TOKEN_URL,
    VenueUnavailableError,
    consent_url,
    exchange_code,
)
from app.infra.clients.secrets import SealError, seal, sealed_value, unseal, unsealed_value

KEY = Fernet.generate_key().decode()
OTHER_KEY = Fernet.generate_key().decode()
REDIRECT = "https://api.example.test/api/v1/callbacks/google/calendar"


def query_of(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


# --------------------------------------------------------------------------
# The consent request
# --------------------------------------------------------------------------


def test_the_consent_asks_for_freebusy_and_nothing_else() -> None:
    """One scope, which is what makes the consent screen say one thing."""
    asked = query_of(consent_url(client_id="cid", redirect_uri=REDIRECT, state="s"))

    # **The literal, not the constant.** Asserting against `FREEBUSY_SCOPE`
    # compares the ask to itself: widening the constant widens the assertion
    # with it, and the consent screen grows a line no test objected to.
    assert asked["scope"] == "https://www.googleapis.com/auth/calendar.freebusy"
    assert asked["scope"] == FREEBUSY_SCOPE


def test_the_consent_forces_a_fresh_grant() -> None:
    """Without both of these Google returns no refresh token.

    The failure is silent: a 200, an access token, and a connection that works
    for an hour. This is the assertion that stops it shipping.
    """
    asked = query_of(consent_url(client_id="cid", redirect_uri=REDIRECT, state="s"))

    assert asked["access_type"] == "offline"
    assert asked["prompt"] == "consent"


def test_the_consent_does_not_let_an_earlier_grant_widen_it() -> None:
    """`include_granted_scopes` would make the narrow ask a lie."""
    asked = query_of(consent_url(client_id="cid", redirect_uri=REDIRECT, state="s"))

    assert "include_granted_scopes" not in asked


def test_the_consent_carries_the_state_and_the_redirect() -> None:
    asked = query_of(consent_url(client_id="cid", redirect_uri=REDIRECT, state="sealed-thing"))

    assert asked["state"] == "sealed-thing"
    assert asked["redirect_uri"] == REDIRECT
    assert asked["client_id"] == "cid"
    assert asked["response_type"] == "code"


# --------------------------------------------------------------------------
# The exchange
# --------------------------------------------------------------------------


def exchanging(handler: object) -> dict[str, object]:
    """Run `exchange_code` against a transport that records the request."""
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    with httpx.Client(transport=transport) as client:
        return exchange_code(
            code="the-code",
            client_id="cid",
            client_secret="secret",  # noqa: S106
            redirect_uri=REDIRECT,
            client=client,
        )


def test_the_exchange_sends_the_grant_google_expects() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = dict(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"refresh_token": "rt", "access_type": "offline"})

    exchanging(handler)

    assert seen["url"] == GOOGLE_TOKEN_URL
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["grant_type"] == ["authorization_code"]
    assert body["code"] == ["the-code"]
    # **The same redirect as the consent.** Google compares them and refuses the
    # pair when they differ; one constant is what stops the two drifting.
    assert body["redirect_uri"] == [REDIRECT]


def test_a_response_without_a_refresh_token_is_refused() -> None:
    """The 200 that would otherwise become a connection that dies in an hour."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "at", "expires_in": 3599})

    with pytest.raises(VenueUnavailableError, match="no refresh token"):
        exchanging(handler)


def test_google_refusing_the_code_is_refused() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    with pytest.raises(VenueUnavailableError, match="refused"):
        exchanging(handler)


def test_a_response_that_is_not_json_is_refused() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>a proxy said something</html>")

    with pytest.raises(VenueUnavailableError):
        exchanging(handler)


# --------------------------------------------------------------------------
# The seal
# --------------------------------------------------------------------------


def test_a_sealed_token_round_trips() -> None:
    assert unseal(seal("refresh-token", key=KEY), key=KEY) == "refresh-token"


def test_sealing_hides_the_value() -> None:
    """Obvious, and worth an assertion: this is the whole reason it exists."""
    assert "refresh-token" not in seal("refresh-token", key=KEY)


def test_another_key_cannot_open_it() -> None:
    with pytest.raises(SealError):
        unseal(seal("refresh-token", key=KEY), key=OTHER_KEY)


def test_a_tampered_token_cannot_be_opened() -> None:
    sealed = seal("refresh-token", key=KEY)
    with pytest.raises(SealError):
        unseal(sealed[:-4] + "AAAA", key=KEY)


def test_a_sealed_object_round_trips() -> None:
    assert unsealed_value(sealed_value({"user_id": "u"}, key=KEY), key=KEY) == {"user_id": "u"}


def test_a_state_older_than_its_ttl_is_refused() -> None:
    """The window that makes a `state` from a browser history worthless."""
    sealed = sealed_value({"user_id": "u"}, key=KEY)

    with pytest.raises(SealError, match="expired"):
        # Fernet stamps the token; asking for a zero-second window ages it past
        # its own timestamp on the next tick rather than needing a clock stub.
        unsealed_value(sealed, key=KEY, ttl=-1)


def test_a_state_carrying_something_other_than_an_object_is_refused() -> None:
    with pytest.raises(SealError, match="object"):
        unsealed_value(seal(json.dumps(["not", "an", "object"]), key=KEY), key=KEY)


def test_no_key_is_an_operator_fault_rather_than_a_seal_failure() -> None:
    """`ConfigurationError`, not `SealError` — nothing was sealed to fail."""
    with pytest.raises(ConfigurationError):
        seal("anything", key=None)


def test_a_malformed_key_is_an_operator_fault() -> None:
    with pytest.raises(ConfigurationError, match="valid Fernet key"):
        seal("anything", key="not-a-fernet-key")
