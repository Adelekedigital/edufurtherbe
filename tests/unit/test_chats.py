"""The chat rules that need no database (ADR 0010; decision 301)."""

import datetime as dt
from uuid import uuid4

import pytest

from app.domain.chats import (
    CONNECTING_STATUSES,
    ELLIPSIS,
    PREVIEW_LENGTH,
    SESSION_WINDOW,
    SESSION_WINDOW_STATUSES,
    ChatLock,
    chat_lock,
    file_storage_key,
    preview,
)
from app.domain.enums import SessionStatus
from app.infra.db.chat_writer import can_message


def test_m0_is_every_booked_session_and_nothing_never_agreed() -> None:
    assert set(CONNECTING_STATUSES) == {
        SessionStatus.CONFIRMED,
        SessionStatus.COMPLETED,
        SessionStatus.NO_SHOW,
        SessionStatus.CANCELLED,
    }


def test_the_window_is_fifteen_minutes_and_never_a_cancelled_session() -> None:
    assert dt.timedelta(minutes=15) == SESSION_WINDOW
    assert set(SESSION_WINDOW_STATUSES) == set(CONNECTING_STATUSES) - {SessionStatus.CANCELLED}


@pytest.mark.parametrize(
    ("closed", "paused", "lock"),
    [
        (False, False, None),
        (False, True, ChatLock.PAUSED),
        (True, False, ChatLock.CLOSED),
        (True, True, ChatLock.CLOSED),
    ],
)
def test_a_closed_account_wins_over_a_pause(
    closed: bool, paused: bool, lock: ChatLock | None
) -> None:
    assert chat_lock(closed=closed, paused=paused) is lock


def test_a_short_text_is_whole_on_one_line() -> None:
    assert preview("Hi Ada,\n\nsee you  soon") == "Hi Ada, see you soon"
    assert preview("x" * PREVIEW_LENGTH) == "x" * PREVIEW_LENGTH


def test_a_long_text_is_cut_at_a_word_within_the_length() -> None:
    text = "word " * 40
    cut = preview(text)

    assert cut.endswith("word" + ELLIPSIS)
    assert len(cut) <= PREVIEW_LENGTH
    assert len(cut) > PREVIEW_LENGTH - len("word ") - 1


def test_the_ellipsis_counts_toward_the_length() -> None:
    # A word that fits with the ellipsis is kept whole; one that ends exactly
    # at the limit does not leave room, so the cut falls before it.
    assert preview("a" * (PREVIEW_LENGTH - 1) + " b") == "a" * (PREVIEW_LENGTH - 1) + ELLIPSIS
    assert preview("c " + "a" * (PREVIEW_LENGTH - 2) + " b") == "c" + ELLIPSIS


def test_one_long_word_is_cut_mid_word() -> None:
    cut = preview("x" * 500)

    assert cut == "x" * (PREVIEW_LENGTH - 1) + ELLIPSIS


def test_a_message_files_key_is_ids_only_under_its_own_prefix() -> None:
    uploader, name = uuid4(), uuid4()

    assert file_storage_key(uploader, name) == f"messages/{uploader}/{name}"


def _chat(
    role: str = "mentor", *, deleted: bool = False, paused: bool = False
) -> dict[str, object]:
    return {"role": role, "deleted": deleted, "paused": paused}


def test_can_message_is_the_send_s_rule_for_a_mentee() -> None:
    assert can_message(_chat()) is True
    assert can_message(None) is False
    assert can_message(_chat("mentee")) is False
    assert can_message(_chat(paused=True)) is False
    assert can_message(_chat(deleted=True)) is False
