"""The team's note on an admin decision (decision 350): one rule, every decision."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError

from app.api.schemas.admin import BulkDecisionWrite, MentorDecisionWrite
from app.api.schemas.common import TEAM_NOTE_MAX_LENGTH, TeamNote


class Holder(BaseModel):
    note: TeamNote = None


@pytest.mark.parametrize(
    ("sent", "kept"),
    [
        ("  Welcome aboard!  ", "Welcome aboard!"),
        ("Line one\nLine two", "Line one\nLine two"),
        ("   ", None),
        ("", None),
        ("\x00", None),
        (None, None),
        ("x" * TEAM_NOTE_MAX_LENGTH, "x" * TEAM_NOTE_MAX_LENGTH),
        (" " + "x" * TEAM_NOTE_MAX_LENGTH + " ", "x" * TEAM_NOTE_MAX_LENGTH),
    ],
    ids=["trimmed", "lines", "blank", "empty", "nul", "null", "at-limit", "trimmed-to-limit"],
)
def test_a_note_is_trimmed_and_blank_is_none(sent: str | None, kept: str | None) -> None:
    assert Holder(note=sent).note == kept


def test_a_note_over_the_limit_is_refused() -> None:
    with pytest.raises(ValidationError) as raised:
        Holder(note="x" * (TEAM_NOTE_MAX_LENGTH + 1))
    assert raised.value.errors()[0]["loc"] == ("note",)


def test_the_decision_note_is_message_and_reason_its_old_name_on_a_decline() -> None:
    assert MentorDecisionWrite().note(approve=True) is None
    assert MentorDecisionWrite(message=" Hi ").note(approve=True) == "Hi"
    assert MentorDecisionWrite(message="Hi").note(approve=False) == "Hi"
    assert MentorDecisionWrite(reason="Too thin").note(approve=False) == "Too thin"
    assert MentorDecisionWrite(message="Same", reason=" Same ").note(approve=False) == "Same"


def test_reason_alone_on_an_approval_is_ignored_as_before() -> None:
    """`reason` was a decline's field: an old client's leftover decline text
    must never reach an approval email."""
    assert MentorDecisionWrite(reason="belongs to a decline").note(approve=True) is None


def test_message_and_reason_that_disagree_are_refused_at_message() -> None:
    with pytest.raises(ValidationError) as raised:
        MentorDecisionWrite(message="One", reason="Two")
    assert [error["loc"] for error in raised.value.errors()] == [("message",)]


def test_an_over_long_message_is_refused_at_message() -> None:
    with pytest.raises(ValidationError) as raised:
        MentorDecisionWrite(message="x" * (TEAM_NOTE_MAX_LENGTH + 1))
    assert [error["loc"] for error in raised.value.errors()] == [("message",)]


def test_the_bulk_note_follows_the_same_rule() -> None:
    ids = [uuid4()]
    assert BulkDecisionWrite(user_ids=ids).message is None
    assert BulkDecisionWrite(user_ids=ids, message="  ").message is None
    assert BulkDecisionWrite(user_ids=ids, message=" Welcome ").message == "Welcome"
    with pytest.raises(ValidationError):
        BulkDecisionWrite(user_ids=ids, message="x" * (TEAM_NOTE_MAX_LENGTH + 1))


def test_reason_is_published_as_deprecated() -> None:
    properties = MentorDecisionWrite.model_json_schema()["properties"]
    assert properties["reason"].get("deprecated") is True
    assert "deprecated" not in properties["message"]
    assert properties["message"]["anyOf"][0]["maxLength"] == TEAM_NOTE_MAX_LENGTH
