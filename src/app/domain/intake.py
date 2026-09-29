"""Product rules for the intake form.

**One constant, and it lives here because it is a decision rather than a
mechanism.** `MAX_QUESTIONS` is what the product says a mentor may ask, not
something the database or the API layer knows — and the layer check is what
found that out: `api/routes/me_intake.py` importing it from `infra/` was an
`api` -> `infra` violation, which is the boundary doing its job rather than an
inconvenience to route around.

There is no column to hold this. "At most five rows in a group" is not
expressible as a `CHECK`, which sees one row, or as a unique index, which
enforces distinctness rather than cardinality. The only database mechanism is a
counting trigger — which is what #107 removed from this schema — so the rule is
enforced in the store and the count races. That cost is named at the call site.

**`answer_problems` is the rule for a mentee's answers at booking** (#207):
pure, so every refusal is a case in a table rather than a round trip.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from app.domain.enums import QuestionType

#: At most five questions on one offering's form.
#:
#: An intake form a mentee abandons is worse than no form. The number is the
#: product's answer to that and is not derived from anything, so changing it is
#: a one-line edit here rather than a migration — which is the whole reason a
#: product rule does not belong in a constraint.
MAX_QUESTIONS = 5


#: A choice question offers at least two options — one is not a choice — and at
#: most ten, past which a form stops being answerable on a phone (#200).
MIN_OPTIONS = 2
MAX_OPTIONS = 10
#: One option's text. Long enough for "Post-submission, waiting for interviews".
MAX_OPTION_LENGTH = 200

#: One text answer, the same bound a booking message has.
MAX_ANSWER_LENGTH = 2000


@dataclass(frozen=True, slots=True)
class AskedQuestion:
    """One live question on the offering being booked."""

    id: UUID
    question_type: QuestionType
    is_required: bool
    allows_multiple: bool
    option_ids: frozenset[UUID]


@dataclass(frozen=True, slots=True)
class GivenAnswer:
    """One answer as the mentee sent it."""

    question_id: UUID
    text: str | None
    option_ids: tuple[UUID, ...] | None
    file_id: UUID | None = None


def answer_problems(
    asked: Sequence[AskedQuestion],
    given: Sequence[GivenAnswer],
    *,
    require_answers: bool,
    usable_files: frozenset[UUID] = frozenset(),
) -> list[tuple[str, str]]:
    """Every problem with these answers, as `(pointer, message)` — none if valid.

    **Answers only to the offering's own live questions**, each once, in the
    form its type takes: text for `free_text`; `option_ids` for `multi_choice`,
    exactly one unless the question allows several, every one an option **of
    that question**; `file_id` for `file_upload`, one of ``usable_files`` — the
    caller's own uploads that answer nothing yet — and each file once. Then
    every required question must be answered, whatever its type. The option
    and file checks are what stop an id from another form, or another user,
    being stored against this booking.

    **`require_answers` switches off only the last step** — a required question
    left unanswered, file questions included. It is configuration
    (`require_intake_answers`) because the frontend that collects answers ships
    after this rule does; an answer that is sent is held to the form regardless.
    """
    questions = {q.id: q for q in asked}
    problems: list[tuple[str, str]] = []
    seen: set[UUID] = set()
    files: set[UUID] = set()
    for index, answer in enumerate(given):
        at = f"/answers/{index}"
        question = questions.get(answer.question_id)
        if question is None:
            problems.append((f"{at}/question_id", "not a question this offering asks"))
            continue
        if answer.question_id in seen:
            problems.append((f"{at}/question_id", "this question is answered more than once"))
            continue
        seen.add(answer.question_id)
        forms = (answer.text, answer.option_ids, answer.file_id)
        if sum(form is not None for form in forms) != 1:
            problems.append((at, "give exactly one of `text`, `option_ids` or `file_id`"))
            continue
        if question.question_type is QuestionType.FREE_TEXT:
            if answer.text is None:
                problems.append((at, "this question takes `text`"))
        elif question.question_type is QuestionType.FILE_UPLOAD:
            if answer.file_id is None:
                problems.append((at, "this question takes `file_id`"))
            elif answer.file_id not in usable_files:
                problems.append((f"{at}/file_id", "not a file you uploaded, or already used"))
            elif answer.file_id in files:
                problems.append((f"{at}/file_id", "this file already answers another question"))
            else:
                files.add(answer.file_id)
        elif answer.option_ids is None:
            problems.append((at, "this question takes `option_ids`"))
        else:
            chosen = answer.option_ids
            if not chosen:
                problems.append((f"{at}/option_ids", "choose at least one option"))
            elif not question.allows_multiple and len(chosen) != 1:
                problems.append((f"{at}/option_ids", "this question takes exactly one option"))
            elif len(set(chosen)) != len(chosen):
                problems.append((f"{at}/option_ids", "an option is chosen more than once"))
            elif not set(chosen) <= question.option_ids:
                problems.append((f"{at}/option_ids", "not an option of this question"))
    for question in asked:
        if require_answers and question.is_required and question.id not in seen:
            problems.append(("/answers", f"question {question.id} is required"))
    return problems
