"""The published `/me/mentor-matches` contract (decision 265)."""

from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.main import create_app


def _spec() -> dict[str, Any]:
    return create_app(Settings(_env_file=None)).openapi()  # type: ignore[call-arg]


def test_the_card_the_reasons_and_the_verdict_are_required() -> None:
    schemas = _spec()["components"]["schemas"]

    assert set(schemas["MentorMatchRead"]["required"]) == {"mentor", "reasons"}
    assert set(schemas["MentorMatchesRead"]["required"]) == {"matches", "exact", "based_on"}


def test_reasons_are_a_union_discriminated_by_code() -> None:
    reasons = _spec()["components"]["schemas"]["MentorMatchRead"]["properties"]["reasons"]

    assert set(reasons["items"]["discriminator"]["mapping"]) == {
        "help_areas",
        "field",
        "origin",
        "study_country",
        "degree",
    }


def test_the_route_is_documented_with_its_failures() -> None:
    get = _spec()["paths"]["/api/v1/me/mentor-matches"]["get"]

    assert get["summary"] and get["description"]
    assert {"200", "401", "422"} <= set(get["responses"])
    params = {p["name"]: p for p in get["parameters"]}
    assert params["limit"]["schema"]["minimum"] == 1
    assert params["limit"]["schema"]["maximum"] == 10
    assert params["limit"]["schema"]["default"] == 2
