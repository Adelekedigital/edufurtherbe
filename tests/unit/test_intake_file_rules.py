"""Intake files: what is accepted, how it is named, and when it expires.

Pure rules, so every refusal is a row in a table rather than an upload.
"""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

import pytest
from pydantic import ValidationError as SettingsError

from app.api.limits import MAX_BODY_BYTES
from app.core.config import INTAKE_FILE_CEILING, Settings
from app.domain import intake_files
from app.domain.enums import IntakeFileType, QuestionType
from app.domain.intake import AskedQuestion, GivenAnswer, answer_problems
from app.domain.intake_files import (
    clean_filename,
    content_disposition,
    file_type,
    retention_cutoff,
    storage_key,
    unused_cutoff,
)
from conftest import PDF_BYTES, docx_bytes

# --------------------------------------------------------------------------
# The type, from the bytes
# --------------------------------------------------------------------------


def test_a_pdf_is_known_by_its_magic_number() -> None:
    assert file_type(PDF_BYTES) is IntakeFileType.PDF


def test_a_word_document_is_known_by_its_declared_main_part() -> None:
    assert file_type(docx_bytes()) is IntakeFileType.DOCX


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"hello, I am a CV", id="plain-text"),
        pytest.param(b"", id="empty"),
        pytest.param(b"PK\x03\x04not really a zip", id="zip-magic-only"),
        pytest.param(b"\x89PNG\r\n\x1a\n", id="png"),
        pytest.param(
            docx_bytes(main="application/vnd.ms-word.document.macroEnabled.main+xml"),
            id="docm-with-macros",
        ),
        pytest.param(
            docx_bytes(
                main="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
                parts=("xl/workbook.xml",),
            ),
            id="xlsx",
        ),
        pytest.param(docx_bytes(parts=()), id="no-document-part"),
    ],
)
def test_anything_else_is_refused(payload: bytes) -> None:
    assert file_type(payload) is None


def test_an_archive_with_too_many_entries_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(intake_files, "MAX_DOCX_ENTRIES", 5)
    assert file_type(docx_bytes(extra_entries=3)) is IntakeFileType.DOCX
    assert file_type(docx_bytes(extra_entries=4)) is None


def test_an_archive_declaring_too_much_once_inflated_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = docx_bytes()
    monkeypatch.setattr(intake_files, "MAX_DOCX_INFLATED", 10_000)
    assert file_type(payload) is IntakeFileType.DOCX
    monkeypatch.setattr(intake_files, "MAX_DOCX_INFLATED", 100)
    assert file_type(payload) is None


def test_an_oversized_content_types_part_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(intake_files, "MAX_CONTENT_TYPES_BYTES", 20)
    assert file_type(docx_bytes()) is None


# --------------------------------------------------------------------------
# The name, in and out
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "kind", "stored"),
    [
        ("My CV.pdf", IntakeFileType.PDF, "My CV.pdf"),
        ("essay.DOCX", IntakeFileType.DOCX, "essay.docx"),
        ("../../etc/passwd.pdf", IntakeFileType.PDF, "passwd.pdf"),
        ("C:\\Users\\me\\cv.pdf", IntakeFileType.PDF, "cv.pdf"),
        ("a\r\nContent-Type: x.pdf", IntakeFileType.PDF, "aContent-Type: x.pdf"),
        ("cv.exe", IntakeFileType.PDF, "cv.exe.pdf"),
        ("...hidden", IntakeFileType.DOCX, "hidden.docx"),
        ("", IntakeFileType.PDF, "file.pdf"),
        (None, IntakeFileType.DOCX, "file.docx"),
        (".pdf", IntakeFileType.PDF, "file.pdf"),
        ("Résumé.pdf", IntakeFileType.PDF, "Résumé.pdf"),
    ],
)
def test_the_filename_is_cleaned_and_takes_the_real_extension(
    given: str | None, kind: IntakeFileType, stored: str
) -> None:
    assert clean_filename(given, kind) == stored


def test_a_long_filename_is_cut_to_the_column_keeping_its_extension() -> None:
    stored = clean_filename("x" * 400 + ".pdf", IntakeFileType.PDF)
    assert len(stored) == 255
    assert stored.endswith(".pdf")


def test_the_download_header_cannot_be_broken_out_of() -> None:
    header = content_disposition('a"b\\c;d%e Résumé.pdf')
    assert header.startswith('attachment; filename="a_b_c_d_e R_sum_.pdf"; ')
    assert header.endswith("filename*=UTF-8''a%22b%5Cc%3Bd%25e%20R%C3%A9sum%C3%A9.pdf")
    assert "\r" not in content_disposition("a\r\nb.pdf")
    assert "\n" not in content_disposition("a\r\nb.pdf")


def test_the_storage_key_is_ids_only() -> None:
    uploader, file_id = uuid4(), uuid4()
    assert storage_key(uploader, file_id) == f"{uploader}/{file_id}"


# --------------------------------------------------------------------------
# When it expires
# --------------------------------------------------------------------------

NOW = dt.datetime(2026, 9, 29, 12, tzinfo=dt.UTC)


def test_no_retention_period_keeps_every_file() -> None:
    assert retention_cutoff(NOW, None) is None


def test_retention_counts_days_back_from_now() -> None:
    assert retention_cutoff(NOW, 30) == NOW - dt.timedelta(days=30)


def test_an_unused_upload_expires_after_its_hours() -> None:
    assert unused_cutoff(NOW, 24) == NOW - dt.timedelta(hours=24)


# --------------------------------------------------------------------------
# The configuration
# --------------------------------------------------------------------------


def test_the_file_ceiling_fits_inside_the_request_body_limit() -> None:
    assert INTAKE_FILE_CEILING < MAX_BODY_BYTES
    assert Settings(_env_file=None).intake_file_max_bytes == 5 * 1024 * 1024


def test_a_file_limit_above_the_ceiling_is_refused() -> None:
    with pytest.raises(SettingsError):
        Settings(_env_file=None, intake_file_max_bytes=INTAKE_FILE_CEILING + 1)


def test_blank_retention_means_keep_forever_and_zero_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTAKE_FILE_RETENTION_DAYS", "")
    monkeypatch.setenv("SUPABASE_INTAKE_BUCKET", " ")
    settings = Settings(_env_file=None)
    assert settings.intake_file_retention_days is None
    assert settings.supabase_intake_bucket is None
    monkeypatch.setenv("INTAKE_FILE_RETENTION_DAYS", "0")
    with pytest.raises(SettingsError):
        Settings(_env_file=None)
    monkeypatch.setenv("INTAKE_FILE_RETENTION_DAYS", "90")
    assert Settings(_env_file=None).intake_file_retention_days == 90


# --------------------------------------------------------------------------
# A file answer at booking
# --------------------------------------------------------------------------

UPLOAD = AskedQuestion(
    id=uuid4(),
    question_type=QuestionType.FILE_UPLOAD,
    is_required=True,
    allows_multiple=False,
    option_ids=frozenset(),
)
SECOND_UPLOAD = AskedQuestion(
    id=uuid4(),
    question_type=QuestionType.FILE_UPLOAD,
    is_required=False,
    allows_multiple=False,
    option_ids=frozenset(),
)
MINE = uuid4()


def file_answer(question: AskedQuestion, file_id: object = MINE) -> GivenAnswer:
    return GivenAnswer(question_id=question.id, text=None, option_ids=None, file_id=file_id)  # type: ignore[arg-type]


def test_a_file_the_caller_may_use_answers_a_file_question() -> None:
    assert (
        answer_problems(
            [UPLOAD], [file_answer(UPLOAD)], require_answers=True, usable_files=frozenset({MINE})
        )
        == []
    )


def test_a_file_the_caller_may_not_use_is_refused() -> None:
    assert answer_problems(
        [UPLOAD],
        [file_answer(UPLOAD, uuid4())],
        require_answers=True,
        usable_files=frozenset({MINE}),
    ) == [("/answers/0/file_id", "not a file you uploaded, or already used")]


def test_one_file_cannot_answer_two_questions() -> None:
    problems = answer_problems(
        [UPLOAD, SECOND_UPLOAD],
        [file_answer(UPLOAD), file_answer(SECOND_UPLOAD)],
        require_answers=True,
        usable_files=frozenset({MINE}),
    )
    assert problems == [("/answers/1/file_id", "this file already answers another question")]


def test_text_for_a_file_question_is_refused() -> None:
    answer = GivenAnswer(question_id=UPLOAD.id, text="my cv", option_ids=None)
    assert answer_problems(
        [UPLOAD], [answer], require_answers=True, usable_files=frozenset({MINE})
    ) == [("/answers/0", "this question takes `file_id`")]


def test_a_file_and_text_together_are_refused() -> None:
    answer = GivenAnswer(question_id=UPLOAD.id, text="x", option_ids=None, file_id=MINE)
    assert answer_problems(
        [UPLOAD], [answer], require_answers=True, usable_files=frozenset({MINE})
    ) == [("/answers/0", "give exactly one of `text`, `option_ids` or `file_id`")]


def test_a_required_file_question_must_be_answered() -> None:
    assert answer_problems([UPLOAD], [], require_answers=True, usable_files=frozenset({MINE})) == [
        ("/answers", f"question {UPLOAD.id} is required")
    ]


def test_a_required_file_question_waits_while_enforcement_is_off() -> None:
    """The switch (#283) covers file questions through the same final step."""
    assert answer_problems([UPLOAD], [], require_answers=False, usable_files=frozenset()) == []
