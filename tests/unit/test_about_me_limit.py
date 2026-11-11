"""The bio limit and its published bound (decision 263). No database needed."""

from __future__ import annotations

from typing import Any

from app.domain.profile_text import ABOUT_ME_MAX_LENGTH


def test_the_limit_is_fifteen_hundred() -> None:
    assert ABOUT_ME_MAX_LENGTH == 1500


def test_the_spec_publishes_the_limit(openapi_spec: dict[str, Any]) -> None:
    spec = openapi_spec
    about = spec["components"]["schemas"]["UserProfileWrite"]["properties"]["about_me"]
    lengths = [part.get("maxLength") for part in about.get("anyOf", [about])]
    assert ABOUT_ME_MAX_LENGTH in lengths
