"""Every read of a live account's handle shares one rule (#684, rule 8): a
string, never empty, never null. A missing one fails validation loudly rather
than reaching the frontend as a `"None"` or empty `/mentors/` link."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from app.api.schemas.admin_mentors import AdminMentorRead, AdminMentorRowRead
from app.api.schemas.common import Handle
from app.api.schemas.mentors import MentorPublicRead, MentorSummaryRead
from app.api.schemas.user import UserRead

HANDLE = TypeAdapter(Handle)


def test_a_handle_is_accepted_as_it_is() -> None:
    assert HANDLE.validate_python("user-k3f9x2ab") == "user-k3f9x2ab"
    assert HANDLE.validate_python("ada-okafor") == "ada-okafor"


@pytest.mark.parametrize("missing", [None, ""])
def test_a_missing_handle_fails_loudly(missing: str | None) -> None:
    with pytest.raises(ValidationError):
        HANDLE.validate_python(missing)


@pytest.mark.parametrize(
    ("model", "field"),
    [
        (UserRead, "slug"),
        (MentorSummaryRead, "slug"),
        (MentorPublicRead, "slug"),
        (AdminMentorRowRead, "handle"),
        (AdminMentorRead, "handle"),
    ],
)
def test_every_live_account_read_requires_the_handle(model: type, field: str) -> None:
    schema = model.model_json_schema()
    assert field in schema["required"]
    assert schema["properties"][field] == {
        **schema["properties"][field],
        "type": "string",
        "minLength": 1,
    }
