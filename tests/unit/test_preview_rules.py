"""The pure rules behind Preview as user (decision 309): when a preview lapses,
which grants hold a permission, the request shapes, and the sign-in claim."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.api.schemas.preview import PreviewExit, PreviewPageView, PreviewStart
from app.domain.admin_permissions import EVERY_GRANT, PREVIEW, REVEAL, holds_any
from app.domain.enums import AdminRole, PreviewEndReason
from app.domain.preview import IDLE_TIMEOUT, MAX_LENGTH, PAGE_PATH_MAX_LENGTH, expires_at, lapse
from app.infra.auth.dev_tokens import mint_dev_token
from app.infra.auth.supabase import SupabaseTokenVerifier

START = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
SECRET = "unit-test-signing-secret-of-enough-length"  # noqa: S105

# --------------------------------------------------------------------------
# The lapse rule
# --------------------------------------------------------------------------


def test_the_limits_are_thirty_minutes_idle_and_two_hours_long() -> None:
    assert timedelta(minutes=30) == IDLE_TIMEOUT
    assert timedelta(hours=2) == MAX_LENGTH
    assert expires_at(START) == START + timedelta(hours=2)


def test_a_preview_holds_until_its_idle_limit_and_lapses_at_it() -> None:
    idle_at = START + IDLE_TIMEOUT

    assert lapse(started_at=START, last_active_at=START, now=idle_at - timedelta(seconds=1)) is None
    assert lapse(started_at=START, last_active_at=START, now=idle_at) == (
        PreviewEndReason.IDLE,
        idle_at,
    )


def test_a_busy_preview_lapses_at_two_hours() -> None:
    end = START + MAX_LENGTH
    active = end - timedelta(minutes=1)

    assert lapse(started_at=START, last_active_at=active, now=end - timedelta(seconds=1)) is None
    assert lapse(started_at=START, last_active_at=active, now=end) == (
        PreviewEndReason.MAX_LENGTH,
        end,
    )


def test_the_limit_that_came_first_is_the_reason_and_its_moment() -> None:
    """Read long after both passed: idle came first, so idle, then."""
    active = START + timedelta(minutes=10)
    late = START + timedelta(hours=5)

    assert lapse(started_at=START, last_active_at=active, now=late) == (
        PreviewEndReason.IDLE,
        active + IDLE_TIMEOUT,
    )
    # Active right up to the end: the length came first.
    busy = START + MAX_LENGTH - timedelta(minutes=5)
    assert lapse(started_at=START, last_active_at=busy, now=late) == (
        PreviewEndReason.MAX_LENGTH,
        START + MAX_LENGTH,
    )


# --------------------------------------------------------------------------
# Permissions
# --------------------------------------------------------------------------


def test_every_grant_previews_and_reveals_today() -> None:
    assert frozenset(AdminRole) == EVERY_GRANT == PREVIEW == REVEAL


@pytest.mark.parametrize("role", list(AdminRole))
def test_each_live_grant_holds_preview(role: AdminRole) -> None:
    assert holds_any([role.value], PREVIEW) is True


def test_no_grant_holds_nothing_and_super_admin_holds_everything() -> None:
    assert holds_any([], PREVIEW) is False
    assert holds_any(["mentor_approval"], []) is False
    assert holds_any(["super_admin"], []) is True
    assert holds_any(["limited_access"], [AdminRole.MENTOR_APPROVAL]) is False
    assert holds_any(["limited_access"], [AdminRole.LIMITED_ACCESS]) is True


# --------------------------------------------------------------------------
# Request shapes
# --------------------------------------------------------------------------


def test_a_start_names_exactly_one_user() -> None:
    assert PreviewStart(user_id=uuid4()).email is None
    assert PreviewStart(email="a@example.test").user_id is None
    with pytest.raises(ValidationError, match="exactly one"):
        PreviewStart()
    with pytest.raises(ValidationError, match="exactly one"):
        PreviewStart(user_id=uuid4(), email="a@example.test")


def test_an_end_is_exit_or_sign_out_only() -> None:
    assert PreviewExit(reason="sign_out").reason == PreviewEndReason.SIGN_OUT
    for reason in ("idle", "max_length", "other"):
        with pytest.raises(ValidationError):
            PreviewExit(reason=reason)  # type: ignore[arg-type]


def test_a_page_view_keeps_the_path_only() -> None:
    assert PreviewPageView(path="/mentors/ada?tab=reviews#top").path == "/mentors/ada"
    assert PreviewPageView(path="/home#x?y").path == "/home"
    longest = "/" + "p" * (PAGE_PATH_MAX_LENGTH - 1)
    # The query does not count towards the bound: it is dropped first.
    assert PreviewPageView(path=longest + "?q=" + "x" * 100).path == longest
    for bad in ("", "mentors", "?q=1", longest + "p", "https://evil.test/x"):
        with pytest.raises(ValidationError):
            PreviewPageView(path=bad)


# --------------------------------------------------------------------------
# The sign-in claim
# --------------------------------------------------------------------------


def test_a_token_carries_its_sign_in_and_each_mint_is_its_own() -> None:
    verifier = SupabaseTokenVerifier(secret=SECRET)
    sign_in = uuid4()

    named = verifier.verify(mint_dev_token(uuid4(), secret=SECRET, session_id=str(sign_in)))
    first = verifier.verify(mint_dev_token(uuid4(), secret=SECRET)).session_id
    second = verifier.verify(mint_dev_token(uuid4(), secret=SECRET)).session_id

    assert named.session_id == sign_in
    assert first is not None
    assert second is not None
    assert first != second


@pytest.mark.parametrize("claim", [None, "not-a-uuid", ""])
def test_a_missing_or_malformed_sign_in_reads_as_none(claim: str | None) -> None:
    verifier = SupabaseTokenVerifier(secret=SECRET)

    claims = verifier.verify(mint_dev_token(uuid4(), secret=SECRET, session_id=claim))

    assert claims.session_id is None
