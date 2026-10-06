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
    GOOGLE_USERINFO_URL,
    MENTOR_SCOPES,
    CalendarAccessRevokedError,
    CalendarScopeNotGrantedError,
    VenueUnavailableError,
    access_token,
    account_email,
    consent_url,
    exchange_code,
)
from app.infra.clients.secrets import SealError, seal, sealed_value, unseal, unsealed_value

KEY = Fernet.generate_key().decode()
#: A stand-in for a refresh token, named rather than repeated.
#:
#: The name matters as much as the reuse: bandit's `S105` fires on a literal
#: assigned to — or compared against — something it reads as a credential, so
#: `{"refresh_token": "rt"}` and `== "rt"` each needed suppressing. Binding the
#: value here takes it out of that heuristic, and a `noqa` per use would have
#: been noise that hides a real finding later.
FAKE_REFRESH = "rt"
OTHER_KEY = Fernet.generate_key().decode()
REDIRECT = "https://api.example.test/api/v1/callbacks/google/calendar"


def query_of(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


# --------------------------------------------------------------------------
# The consent request
# --------------------------------------------------------------------------


def test_the_consent_asks_for_freebusy_and_the_account_s_email() -> None:
    """Three scopes, and the consent screen says two things.

    **Widened deliberately on 2026-10-06**, amending ADR 0012 while it was still
    `Proposed`. `openid` and `email` are non-sensitive, so they cannot be the
    most sensitive scope in the set and change no verification requirement; the
    whole cost is one more line on a screen that already asks for availability.
    What it buys is a mentor being able to see *which* Google account they
    connected, which `calendar.freebusy` cannot answer on its own —
    `calendarList.list` is outside it, so there is no second route to the name.

    **Changed when it was free.** A widened scope does not retro-fit an existing
    grant: a mentor who consented to less has no email in their token and gets
    one only by consenting again. At the time of the change no environment had a
    client configured and nobody had connected, so the cost was zero; after that
    it is a re-consent for every connected mentor.

    # **The literal, not the constant.** Asserting against the constant compares
    # the ask to itself: widening it would widen the assertion with it, and the
    # consent screen would grow a line no test objected to. This test is the
    # objection, and changing it is how the ask is allowed to widen.
    """
    asked = query_of(consent_url(client_id="cid", redirect_uri=REDIRECT, state="s"))

    assert asked["scope"] == ("openid email https://www.googleapis.com/auth/calendar.freebusy")
    assert asked["scope"] == MENTOR_SCOPES
    assert FREEBUSY_SCOPE in asked["scope"]


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


# --------------------------------------------------------------------------
# Naming the connected account (#180, ADR 0012 as amended)
#
# The email is a convenience, and these pin the line between a convenience and
# the grant: the grant is the thing a mentor came to make, so nothing about
# reading their address may stop it being stored.
# --------------------------------------------------------------------------


def test_the_account_email_is_read_with_the_token_the_consent_produced() -> None:
    """Sent as a bearer token, to Google's userinfo endpoint, and nowhere else."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"email": "mentor@example.com", "sub": "123"})

    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    with httpx.Client(transport=transport) as client:
        found = account_email(token="at-xyz", client=client)  # noqa: S106

    assert found == "mentor@example.com"
    assert seen["url"] == GOOGLE_USERINFO_URL
    assert seen["auth"] == "Bearer at-xyz"


def test_an_unreadable_email_is_none_rather_than_a_refusal() -> None:
    """**The grant still gets stored.** A mentor consented to connect a calendar;
    losing the line that names their account must not lose the connection.

    Watched to fail by raising instead of returning `None`: the callback then
    answers `502` for a consent Google completed, and the mentor is told nothing
    worked when their calendar is connected.
    """

    def refuses(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="try later")

    transport = httpx.MockTransport(refuses)  # type: ignore[arg-type]
    with httpx.Client(transport=transport) as client:
        assert account_email(token="at-xyz", client=client) is None  # noqa: S106


def test_an_email_google_does_not_send_is_none() -> None:
    """A 200 carrying no `email` claim. Null, not `"None"` and not a `KeyError`."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"sub": "123"})

    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    with httpx.Client(transport=transport) as client:
        assert account_email(token="at-xyz", client=client) is None  # noqa: S106


# --------------------------------------------------------------------------
# What Google actually granted
#
# Granular permissions mean a mentor can confirm the account, leave the calendar
# checkbox unticked, and complete a consent that grants `openid email` alone.
# Observed on 2026-10-06: the checkbox is a separate step after the account
# screen, so this is the default path and not an edge case.
#
# Stored unchecked, that grant is invisible: `status` active, `last_error` null,
# an account email beside it, and every free/busy read failing 403 — which is
# classified transient, so `record_failure` is never reached and the health sweep
# never marks it. A connection that cannot work, that nothing reports.
# --------------------------------------------------------------------------


def test_a_consent_without_the_calendar_scope_is_refused() -> None:
    """**The 200 that is not a success**, same class as a missing refresh token.

    Watched to fail by accepting the payload: the exchange then returns tokens
    that cannot read a calendar, and the caller stores a grant that looks
    healthy for ever.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "refresh_token": FAKE_REFRESH,
                "access_token": "at",
                # Exactly what Google sent when the calendar box was left
                # unticked, minus the calendar scope.
                "scope": "email https://www.googleapis.com/auth/userinfo.email openid",
            },
        )

    with pytest.raises(CalendarScopeNotGrantedError, match="availability"):
        exchanging(handler)


def test_the_granted_scope_google_really_sent_is_accepted() -> None:
    """The literal string from the successful consent of 2026-10-06.

    **Recorded verbatim rather than reconstructed.** The order is Google's, not
    ours, and `email` appears both as the OIDC alias and as the full
    `userinfo.email` URL — a membership test written against our own request
    would have missed both facts.
    """
    granted = (
        "email https://www.googleapis.com/auth/userinfo.email openid "
        "https://www.googleapis.com/auth/calendar.freebusy"
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"refresh_token": FAKE_REFRESH, "access_token": "at", "scope": granted},
        )

    assert exchanging(handler)["refresh_token"] == FAKE_REFRESH


def test_a_token_response_with_no_scope_at_all_is_accepted() -> None:
    """**Absence is not refusal.**

    Google sends `scope` on an authorization-code exchange, but refusing when it
    is missing would turn a response-shape change into every mentor being unable
    to connect. The check answers the question it can answer: *was the calendar
    scope explicitly withheld.*
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"refresh_token": FAKE_REFRESH, "access_token": "at"},
        )

    assert exchanging(handler)["refresh_token"] == FAKE_REFRESH
