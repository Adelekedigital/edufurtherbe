"""A choice question with nothing to choose is never required (help-area questions).

A help-area question's options are the mentor's live help areas, so a mentor who
removes them all leaves it empty. Requiring it then would make the offering
unbookable with no answer a mentee could give.
"""

from __future__ import annotations

from uuid import uuid4

from app.domain.enums import QuestionType
from app.domain.intake import AskedQuestion, GivenAnswer, answer_problems


def _choice(*option_ids: object) -> AskedQuestion:
    return AskedQuestion(
        id=uuid4(),
        question_type=QuestionType.MULTI_CHOICE,
        is_required=True,
        allows_multiple=False,
        option_ids=frozenset(option_ids),  # type: ignore[arg-type]
    )


def test_a_required_choice_with_no_options_is_not_required() -> None:
    assert answer_problems([_choice()], [], require_answers=True) == []


def test_a_required_choice_with_options_is_still_required() -> None:
    question = _choice(uuid4())

    assert answer_problems([question], [], require_answers=True) == [
        ("/answers", f"question {question.id} is required")
    ]


def test_an_empty_choice_still_refuses_an_answer_naming_an_option() -> None:
    question = _choice()

    problems = answer_problems(
        [question],
        [GivenAnswer(question_id=question.id, text=None, option_ids=(uuid4(),))],
        require_answers=True,
    )

    assert problems == [("/answers/0/option_ids", "not an option of this question")]
