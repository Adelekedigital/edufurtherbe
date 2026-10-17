"""The published shape of `PUT /api/v1/me/onboarding` (decision 260)."""

from __future__ import annotations

from app.core.config import Settings
from app.domain.onboarding import STEPS
from app.main import create_app


def test_the_mentee_steps_in_order() -> None:
    assert STEPS == ("help", "plans")


def test_the_published_enum_is_the_one_list() -> None:
    """The field is typed with the domain's `Literal`, so a step added there is
    published without a second list to remember (non-negotiable #8)."""
    schemas = create_app(Settings(_env_file=None)).openapi()["components"]["schemas"]
    body = schemas["OnboardingStepWrite"]

    assert body["properties"]["last_step"]["enum"] == list(STEPS)
    assert body["required"] == ["last_step"]
    assert body["additionalProperties"] is False
