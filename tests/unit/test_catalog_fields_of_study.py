"""The field-of-study suggestions, served publicly at `/catalog/fields-of-study`.

The list only suggests: the mentee's answer is free text (decision 259), so the
load-bearing property here is that **every suggestion can be saved** — a
suggestion the goal write refuses would be a button that fails. The other is
that the path is not swallowed by the generic `/catalog/{catalogue}` lookup,
which answers any unknown name with a 404.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.schemas.profile import GoalWrite
from app.core.config import Settings
from app.domain.goals import FIELD_OF_STUDY_MAX_LENGTH, FIELD_OF_STUDY_SUGGESTIONS
from app.main import create_app

PATH = "/api/v1/catalog/fields-of-study"

#: The owner's list, in the owner's order (2026-10-17). Written out rather than
#: read from the constant, or the test would agree with any edit to it.
EXPECTED = [
    "Business and Management",
    "Economics and Econometrics",
    "Law",
    "Government and Political Science",
    "Computer and Information Science",
    "Mathematics and Statistics",
    "Engineering",
    "Physical Sciences",
    "Biological and Biomedical Sciences",
    "Health Science",
    "Psychology",
    "Social Sciences and History",
    "Education",
    "Communication and Journalism",
    "Visual and Performing Arts",
    "Public Health",
    "Data Science",
    "Arts and Humanities",
    "Architecture and Urban Planning",
    "Agriculture and Environmental Science",
]


def test_the_list_is_served_whole_in_order_with_no_token(client: TestClient) -> None:
    response = client.get(PATH)

    assert response.status_code == 200
    assert response.json() == {
        "data": [{"name": name} for name in EXPECTED],
        "next_cursor": None,
    }


def test_the_answer_may_be_cached_by_anyone(client: TestClient) -> None:
    """The same list for every caller, changing only with a deploy."""
    response = client.get(PATH)

    assert response.headers["cache-control"] == "public, max-age=3600"


@pytest.mark.parametrize("name", FIELD_OF_STUDY_SUGGESTIONS)
def test_every_suggestion_can_be_saved_as_typed(name: str) -> None:
    """The rule a typed answer must pass, applied to each suggestion: picking one
    and saving it must never be a 422."""
    assert len(name) <= FIELD_OF_STUDY_MAX_LENGTH
    assert GoalWrite(field_of_study=name).field_of_study == name


def test_no_two_suggestions_differ_only_in_case() -> None:
    folded = [name.casefold() for name in FIELD_OF_STUDY_SUGGESTIONS]

    assert len(set(folded)) == len(folded) == len(EXPECTED)


def spec() -> dict[str, Any]:
    return dict(create_app(Settings(_env_file=None)).openapi())


def test_the_route_is_its_own_operation_in_the_catalog_tag() -> None:
    """Its own operation, not the generic lookup's: the generic one would
    document a cursor, a `q` and a 404 this list never has."""
    paths = spec()["paths"]
    operation = paths["/api/v1/catalog/fields-of-study"]["get"]

    assert operation["tags"] == ["catalog"]
    assert "security" not in operation
    assert operation["operationId"] != paths["/api/v1/catalog/{catalogue}"]["get"]["operationId"]
    assert operation["summary"]
    assert "never restricts" in operation["description"]
