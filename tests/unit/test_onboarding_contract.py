"""The published shape of `PUT /api/v1/me/onboarding` (decision 260)."""

from __future__ import annotations

from typing import Any

from app.domain.onboarding import STEPS


def test_the_onboarding_steps_in_order() -> None:
    assert STEPS == (
        "help",
        "plans",
        "mentor_help",
        "mentor_education",
        "mentor_about",
        "mentor_hours",
    )


def test_the_published_enum_is_the_one_list(openapi_spec: dict[str, Any]) -> None:
    """The field is typed with the domain's `Literal`, so a step added there is
    published without a second list to remember (non-negotiable #8)."""
    schemas = openapi_spec["components"]["schemas"]
    body = schemas["OnboardingStepWrite"]

    assert body["properties"]["last_step"]["enum"] == list(STEPS)
    assert body["required"] == ["last_step"]
    assert body["additionalProperties"] is False
