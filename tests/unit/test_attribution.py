"""PRD 30 PR 2: where an account and a booking came from, read off what we are sent."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from app.api.schemas.attribution import AcquisitionWrite
from app.api.schemas.sessions import SessionBookingWrite
from app.domain.analytics_events import (
    cancelled_properties,
    requested_properties,
    session_properties,
)
from app.domain.attribution import (
    auth_method_from,
    signup_channel,
    signup_source,
)
from app.domain.enums import AuthMethod, EntryPoint, SignupChannel, SignupSource
from app.infra.auth.dev_tokens import mint_dev_token
from app.infra.auth.supabase import SupabaseTokenVerifier

SECRET = "unit-test-signing-secret-of-enough-length"  # noqa: S105
SESSION = UUID("0190aaaa-0000-7000-8000-0000000000aa")
TYPE = UUID("0190aaaa-0000-7000-8000-0000000000bb")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("campaign", SignupSource.CAMPAIGN),
        (" Referral_Site ", SignupSource.REFERRAL_SITE),
        ("invite", SignupSource.INVITE),
        ("direct", SignupSource.DIRECT),
        ("migrated", SignupSource.DIRECT),
        ("MIGRATED", SignupSource.DIRECT),
        ("facebook", SignupSource.DIRECT),
        ("", SignupSource.DIRECT),
        (None, SignupSource.DIRECT),
        (7, SignupSource.DIRECT),
    ],
)
def test_a_source_is_the_visitors_own_only_when_they_may_claim_it(
    raw: object, expected: SignupSource
) -> None:
    assert signup_source(raw, invited=False) is expected


@pytest.mark.parametrize("raw", ["campaign", "migrated", None, "nonsense"])
def test_a_real_invite_is_an_invite_whatever_the_source_said(raw: object) -> None:
    assert signup_source(raw, invited=True) is SignupSource.INVITE


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("linkedin", SignupChannel.LINKEDIN),
        ("WhatsApp", SignupChannel.WHATSAPP),
        ("newsletter", SignupChannel.NEWSLETTER),
        ("tiktok", SignupChannel.OTHER),
        ("<script>", SignupChannel.OTHER),
        ("?", SignupChannel.OTHER),
        (None, None),
        ("  ", None),
    ],
)
def test_a_channel_outside_the_list_is_other_and_none_is_none(
    raw: object, expected: SignupChannel | None
) -> None:
    assert signup_channel(raw) == expected


def test_the_acquisition_body_never_refuses_a_value() -> None:
    body = AcquisitionWrite.model_validate(
        {"source": 12, "channel": ["x"], "referral_code": 5, "utm_medium": "cpc"}
    )

    assert (body.source, body.channel, body.referral_code) == ("?", "?", None)
    assert signup_source(body.source, invited=False) is SignupSource.DIRECT
    assert signup_channel(body.channel) is SignupChannel.OTHER
    assert len(AcquisitionWrite(channel="x" * 5000).channel or "") == 200


@pytest.mark.parametrize(
    ("methods", "providers", "expected"),
    [
        (["otp"], ["email"], AuthMethod.EMAIL_CODE),
        (["magiclink"], ["email"], AuthMethod.EMAIL_CODE),
        (["oauth"], ["google"], AuthMethod.GOOGLE_OAUTH),
        (["oauth"], ["email", "google"], AuthMethod.GOOGLE_OAUTH),
        (["oauth"], ["linkedin_oidc"], None),
        (["password"], ["email"], None),
        ([], [], None),
    ],
)
def test_how_a_sign_in_signed_in(
    methods: list[str], providers: list[str], expected: AuthMethod | None
) -> None:
    assert auth_method_from(methods, providers) == expected


def verify(**claims: object) -> AuthMethod | None:
    token = mint_dev_token(uuid4(), secret=SECRET, **claims)
    return SupabaseTokenVerifier(secret=SECRET).verify(token).auth_method


def test_the_token_says_how_it_signed_in() -> None:
    assert (
        verify(amr=[{"method": "otp", "timestamp": 1}], app_metadata={"providers": ["email"]})
        is AuthMethod.EMAIL_CODE
    )
    assert (
        verify(amr=[{"method": "oauth", "timestamp": 1}], app_metadata={"providers": ["google"]})
        is AuthMethod.GOOGLE_OAUTH
    )


@pytest.mark.parametrize(
    "claims",
    [
        {},
        {"amr": "otp"},
        {"amr": [{"timestamp": 1}, "otp"]},
        {"amr": [{"method": "oauth"}], "app_metadata": "google"},
        {"amr": [{"method": "oauth"}], "app_metadata": {"providers": "google"}},
    ],
)
def test_a_claim_of_another_shape_says_nothing(claims: dict[str, object]) -> None:
    assert verify(**claims) is None


def test_an_entry_point_is_carried_only_when_there_is_one() -> None:
    assert requested_properties(SESSION, TYPE, None) == {
        "session_id": SESSION,
        "session_type_id": TYPE,
    }
    assert requested_properties(SESSION, TYPE, EntryPoint.EXPLORE)["entry_point"] is (
        EntryPoint.EXPLORE
    )
    assert session_properties(SESSION, None) == {"session_id": SESSION}
    assert session_properties(SESSION, EntryPoint.HOME) == {
        "session_id": SESSION,
        "entry_point": EntryPoint.HOME,
    }
    assert cancelled_properties(SESSION, "mentee", "other", EntryPoint.MENTOR_PROFILE) == {
        "session_id": SESSION,
        "by": "mentee",
        "reason_code": "other",
        "entry_point": EntryPoint.MENTOR_PROFILE,
    }
    assert "entry_point" not in cancelled_properties(SESSION, "mentee", "other", None)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("explore", EntryPoint.EXPLORE),
        ("rebook_after_review", EntryPoint.REBOOK_AFTER_REVIEW),
        ("messages", None),
        ("nonsense", None),
        (None, None),
    ],
)
def test_a_bookings_entry_point_outside_the_list_is_dropped_not_refused(
    raw: object, expected: EntryPoint | None
) -> None:
    body = SessionBookingWrite.model_validate(
        {"session_type_id": str(TYPE), "starts_at": "2026-12-01T10:00:00Z", "entry_point": raw}
    )

    assert body.entry_point == expected


def test_the_booking_contract_lists_the_entry_points() -> None:
    schema = SessionBookingWrite.model_json_schema()
    published = schema["$defs"]["EntryPoint"]["enum"]

    assert published == [point.value for point in EntryPoint]
    assert "messages" not in published


def test_the_consent_answer_is_optional_on_me(openapi_spec: dict[str, Any]) -> None:
    """Not in `/me`'s required list, as the other optional fields are not: the
    frontend's type-checked `/me` mock would break otherwise."""
    schema = openapi_spec["components"]["schemas"]["UserRead"]

    assert "analytics_consent" in schema["properties"]
    assert "analytics_consent" not in schema.get("required", [])
