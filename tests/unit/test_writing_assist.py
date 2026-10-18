"""The writing assistant's pure rules (decision 267): versions, bodies, keys, answers."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.domain.enums import WritingAssistField, WritingAssistMode
from app.domain.profile_text import ABOUT_ME_MAX_LENGTH
from app.domain.writing_assist import (
    BACKGROUND_ATTEMPT,
    DAILY_WINDOW,
    FACT_MAX_LENGTH,
    TASKS,
    AssistResult,
    answer_from,
    background_relation_id,
    bio_draft_enabled,
    call_for,
    fits,
    is_known_prompt_version,
    provenance_problem,
    source_for,
    source_size,
    usable_text,
    window_retry_after,
)

MENTEE = WritingAssistField.MENTEE_BIO
MENTOR = WritingAssistField.MENTOR_ABOUT
DRAFT = WritingAssistMode.DRAFT
IMPROVE = WritingAssistMode.IMPROVE


@pytest.mark.parametrize(
    "version",
    # Pattern-based, never a list: the router's v2 and any later bump pass.
    ["bio_draft-v1", "bio_improve-v1", "bio_draft-v2", "bio_improve-v2", "bio_draft-v12"],
)
def test_a_bio_prompt_version_is_known(version: str) -> None:
    assert is_known_prompt_version(version)


@pytest.mark.parametrize(
    "version",
    [
        "mentee_bio_draft-v1",  # the pattern's shape, but no task of ours
        "scholarship_extraction-v1",
        "bio_draft",
        "bio_draft-v",
        "Bio_draft-v1",
        "bio_draft-v1 ",
        "bio_draft-vx",
        "",
        "bio_draft-v" + "1" * 54,  # 65 characters
    ],
)
def test_anything_else_is_not(version: str) -> None:
    assert not is_known_prompt_version(version)


def test_the_longest_known_version_is_sixty_four_characters() -> None:
    longest = "bio_draft-v" + "1" * 53

    assert len(longest) == 64
    assert is_known_prompt_version(longest)


def test_every_body_names_the_role_and_the_attempt() -> None:
    """The router does not check `role`, so it is asserted here (router, 2026-10-17)."""
    assert source_for(MENTEE, DRAFT, attempt=1, facts={})["role"] == "mentee"
    assert source_for(MENTOR, DRAFT, attempt=2, facts={})["role"] == "mentor"
    assert source_for(MENTEE, IMPROVE, attempt=3, text="x")["role"] == "mentee"
    assert source_for(MENTOR, IMPROVE, attempt=4, text="x") == {
        "role": "mentor",
        "attempt": 4,
        "text": "x",
    }


def test_an_improve_sends_only_the_text_even_when_handed_facts() -> None:
    body = source_for(MENTEE, IMPROVE, attempt=1, facts={"origin": "Ghana"}, text="mine")

    assert body == {"role": "mentee", "attempt": 1, "text": "mine"}


def test_a_draft_sends_the_facts_it_has_and_omits_the_empty_ones() -> None:
    body = source_for(
        MENTEE,
        DRAFT,
        attempt=1,
        facts={"origin": "Ghana", "degree": None, "help_areas": [], "destinations": ["Canada"]},
    )

    assert body == {"role": "mentee", "attempt": 1, "origin": "Ghana", "destinations": ["Canada"]}


def test_the_key_is_the_same_for_the_same_call_and_new_for_any_change() -> None:
    user = uuid4()
    body = source_for(MENTEE, IMPROVE, attempt=1, text="mine")
    key = call_for(user, MENTEE, IMPROVE, body, "r").idempotency_key

    assert call_for(user, MENTEE, IMPROVE, dict(body), "other").idempotency_key == key
    assert len(key) == 64
    changed = [
        call_for(uuid4(), MENTEE, IMPROVE, body, "r"),
        call_for(user, MENTOR, IMPROVE, body, "r"),
        call_for(user, MENTEE, DRAFT, body, "r"),
        call_for(user, MENTEE, IMPROVE, {**body, "attempt": 2}, "r"),
        call_for(user, MENTEE, IMPROVE, {**body, "text": "other"}, "r"),
    ]
    assert key not in {call.idempotency_key for call in changed}


def test_a_call_names_the_task_for_its_mode() -> None:
    assert call_for(uuid4(), MENTOR, DRAFT, {}, "r").task == "bio_draft"
    assert call_for(uuid4(), MENTOR, IMPROVE, {}, "r").task == "bio_improve"


def test_the_size_counts_escaped_non_ascii_as_the_router_does() -> None:
    body = {"text": "é"}

    assert source_size(body) == len(json.dumps(body).encode()) == len('{"text": "\\u00e9"}')


def test_an_answer_is_trimmed_and_keeps_its_paragraphs() -> None:
    raw = "  First line.\r\nSecond​ line.‮\n\nThird.  \n"

    assert usable_text(raw, MENTEE) == "First line.\nSecond line.\n\nThird."


@pytest.mark.parametrize("raw", ["", "   \n  ", "​​", None, 7, {"text": "x"}], ids=repr)
def test_no_usable_text_is_no_answer(raw: object) -> None:
    assert usable_text(raw, MENTOR) is None


def test_an_answer_keeps_its_joiners() -> None:
    """ZWJ and ZWNJ are orthography (Persian, Sinhala) and hold emoji sequences."""
    zwj, zwnj = chr(0x200D), chr(0x200C)
    raw = f"Coder {chr(0x1F468)}{zwj}{chr(0x1F4BB)} and {chr(0x645)}{chr(0x6CC)}{zwnj}{chr(0x62E)}"

    assert usable_text(raw, MENTEE) == raw


def test_a_drafts_free_text_facts_are_cut_to_their_bound() -> None:
    body = source_for(
        MENTOR,
        DRAFT,
        attempt=1,
        facts={"study_program": "p" * 5000, "help_areas": ["h" * 5000], "years_of_experience": 4},
    )

    assert body["study_program"] == "p" * FACT_MAX_LENGTH
    assert body["help_areas"] == ["h" * FACT_MAX_LENGTH]
    assert body["years_of_experience"] == 4


def test_an_answer_at_the_bound_is_kept_and_one_over_it_is_not() -> None:
    assert usable_text("a" * ABOUT_ME_MAX_LENGTH, MENTEE) == "a" * ABOUT_ME_MAX_LENGTH
    assert usable_text(" " + "a" * ABOUT_ME_MAX_LENGTH + " ", MENTEE) is not None
    assert usable_text("a" * (ABOUT_ME_MAX_LENGTH + 1), MENTEE) is None


def test_retry_after_is_the_seconds_until_the_call_leaves_the_window() -> None:
    now = datetime(2026, 10, 17, 12, tzinfo=UTC)

    assert window_retry_after(now - DAILY_WINDOW + timedelta(seconds=90.2), now) == 91
    assert window_retry_after(now - DAILY_WINDOW, now) == 1
    assert window_retry_after(now - 2 * DAILY_WINDOW, now) == 1


DRAFTED = {
    "about_me": "A bio.",
    "about_me_drafted": True,
    "about_me_prompt_version": "bio_draft-v1",
}


@pytest.mark.parametrize(
    "sent",
    [
        DRAFTED,
        {**DRAFTED, "about_me_prompt_version": "bio_improve-v3"},
        {"about_me_drafted": False},
        {"about_me_drafted": False, "about_me": None},
        {"about_me": "Mine."},
        {},
    ],
    ids=["drafted", "improved", "own", "own-cleared", "text-only", "nothing"],
)
def test_a_well_formed_provenance_passes(sent: dict[str, object]) -> None:
    assert provenance_problem(sent) is None


@pytest.mark.parametrize(
    ("sent", "pointer"),
    [
        ({"about_me_prompt_version": "bio_draft-v1"}, "/about_me_prompt_version"),
        ({**DRAFTED, "about_me_drafted": False}, "/about_me_prompt_version"),
        ({"about_me": "A bio.", "about_me_drafted": True}, "/about_me_prompt_version"),
        ({**DRAFTED, "about_me_prompt_version": None}, "/about_me_prompt_version"),
        ({**DRAFTED, "about_me_prompt_version": "mentee_bio_draft-v1"}, "/about_me_prompt_version"),
        ({**DRAFTED, "about_me_prompt_version": "bio_draft"}, "/about_me_prompt_version"),
        ({"about_me_drafted": True, "about_me_prompt_version": "bio_draft-v1"}, "/about_me"),
        ({**DRAFTED, "about_me": None}, "/about_me"),
        ({**DRAFTED, "about_me": "  "}, "/about_me"),
        ({"about_me_drafted": None}, "/about_me_drafted"),
    ],
    ids=[
        "version-alone",
        "version-with-false",
        "drafted-without-version",
        "drafted-null-version",
        "unknown-task",
        "malformed",
        "drafted-without-text",
        "drafted-null-text",
        "drafted-blank-text",
        "null-flag",
    ],
)
def test_a_malformed_provenance_is_refused_where_it_is_wrong(
    sent: dict[str, object], pointer: str
) -> None:
    problem = provenance_problem(sent)

    assert problem is not None
    assert problem[0] == pointer


# --------------------------------------------------------------------------
# The background mentee draft (decision 269) and the rules both paths share
# --------------------------------------------------------------------------


def test_the_background_draft_never_shares_a_key_with_a_press() -> None:
    """A press counts from 1, so attempt 0 is the background draft's alone."""
    user = uuid4()
    facts = {"origin": "Nigeria"}
    background = source_for(MENTEE, DRAFT, attempt=BACKGROUND_ATTEMPT, facts=facts)
    first_press = source_for(MENTEE, DRAFT, attempt=1, facts=facts)
    assert BACKGROUND_ATTEMPT == 0
    assert (
        call_for(user, MENTEE, DRAFT, background, "r").idempotency_key
        != call_for(user, MENTEE, DRAFT, first_press, "r").idempotency_key
    )


def test_the_background_relation_is_stable_per_user_and_names_nobody() -> None:
    user = uuid4()
    relation = background_relation_id(user)
    assert relation == background_relation_id(user)
    assert relation != background_relation_id(uuid4())
    assert str(user) not in relation


def test_a_body_fits_up_to_the_tasks_cap_and_not_past_it() -> None:
    cap = TASKS[DRAFT].max_source_bytes
    empty = source_size({"text": ""})
    assert fits({"text": "a" * (cap - empty)}, DRAFT) is True
    assert fits({"text": "a" * (cap - empty + 1)}, DRAFT) is False


def test_an_answer_is_its_trimmed_text_and_version() -> None:
    answer = answer_from(AssistResult(text="  Hi.  ", prompt_version="bio_draft-v1"), MENTEE)
    assert answer == AssistResult(text="Hi.", prompt_version="bio_draft-v1")


@pytest.mark.parametrize(
    "result",
    [
        None,
        AssistResult(text="   ", prompt_version="bio_draft-v1"),
        AssistResult(text="Hi.", prompt_version="summary-v1"),
    ],
)
def test_no_text_or_an_unknown_version_is_no_answer(result: AssistResult | None) -> None:
    assert answer_from(result, MENTEE) is None


def test_the_background_draft_is_off_by_default() -> None:
    assert bio_draft_enabled(Settings(_env_file=None)) is False  # type: ignore[call-arg]
    on = Settings(_env_file=None, mentee_bio_draft_enabled=True)  # type: ignore[call-arg]
    assert bio_draft_enabled(on) is True
