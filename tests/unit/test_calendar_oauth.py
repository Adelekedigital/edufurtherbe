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

from app.api.schemas.availability import CalendarConnectionRead
from app.core.errors import ConfigurationError
from app.domain.availability import CALENDAR_FAILURE_REASONS, CALENDAR_REVOKED
from app.infra.clients.meetings import (
    FREEBUSY_SCOPE,
    GOOGLE_TOKEN_URL,
    CalendarAccessRevokedError,
    VenueUnavailableError,
    access_token,
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


# --------------------------------------------------------------------------
# The failure vocabulary
#
# `last_error` is the one field this service puts in front of a mentor as the
# reason their calendar stopped working, and a client writes copy per value
# (the frontend's Integrations page does). So the set has to stay the same in
# places that cannot see each other: the two writers, and the published
# description a client reads. Non-negotiable #8, the same way
# `SESSION_DURATION_MINUTES` is pinned to its CHECK.
#
# The description is *generated* from `CALENDAR_FAILURE_REASONS`, so a reason
# added there cannot go undescribed. What is left to pin is the two ways that
# generation can be undone by hand, and both have already happened once:
# a writer carrying its own literal, and the description retyped in place.
# --------------------------------------------------------------------------


def test_a_revoked_grant_is_recorded_in_the_vocabulary_s_words() -> None:
    """The writer's message *is* the constant, not a copy that happens to match.

    Watched to fail by changing `CALENDAR_REVOKED` alone: the raise site then
    disagrees with the vocabulary the description is built from, which is
    exactly the drift that let the docstring claim these were Google's words.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    with (
        httpx.Client(transport=transport) as client,
        pytest.raises(CalendarAccessRevokedError) as caught,
    ):
        access_token(
            client_id="cid",
            client_secret="secret",  # noqa: S106
            refresh_token="stale",  # noqa: S106
            client=client,
        )
    assert str(caught.value) == CALENDAR_REVOKED


def test_every_reason_a_mentor_can_be_shown_is_in_the_published_description() -> None:
    """Retype the description in place and this fails.

    **Not a guarantee that the generation is correct** — it is generated from
    the same tuple, so this would agree with it either way. What it catches is
    the description being replaced by a hand-written one, which is how the field
    came to advertise a pair as "Google's words": prose and literals copied to
    where a reader would see them, and then left behind.

    The failure a client meets otherwise is silent: an unrecognised value, and
    copy written for the set that used to be the whole set.
    """
    described = CalendarConnectionRead.model_fields["last_error"].description or ""
    missing = [reason for reason in CALENDAR_FAILURE_REASONS if reason not in described]
    assert not missing, f"undescribed `last_error` values: {missing}"
