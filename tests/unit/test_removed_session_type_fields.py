"""The single `service_offering_id` write field is removed (#274), and hidden.

It stays declared only so that sending it is a `422` pointing at it rather than
a silently ignored key; it must never appear in a published write schema.
"""

from app.api.schemas.session_types import MentorSessionTypePatch, MentorSessionTypeWrite


def test_the_removed_field_is_not_published() -> None:
    for model in (MentorSessionTypeWrite, MentorSessionTypePatch):
        properties = model.model_json_schema()["properties"]
        assert "service_offering_id" not in properties
        assert "service_offering_ids" in properties


def test_the_removed_field_is_never_dumped() -> None:
    """Even a default instance cannot carry it into the store's payload."""
    assert "service_offering_id" not in MentorSessionTypePatch().model_dump()
