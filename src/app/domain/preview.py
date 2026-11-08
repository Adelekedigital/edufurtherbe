"""An admin's read-only preview of the app as one user sees it (decision 309).

The bounds live here once: the request models and the columns' `CHECK`s both
read them, and a migration test pins the live constraints to them. So does the
one rule for when a preview has lapsed, which every reader of a preview applies.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import NamedTuple

from app.domain.enums import PreviewEndReason

#: The longest page path a preview logs, in characters.
PAGE_PATH_MAX_LENGTH = 2048

#: The longest reason an admin may give for a reveal, in characters.
REVEAL_REASON_MAX_LENGTH = 500

#: A preview with no request for this long has ended (`idle`).
IDLE_TIMEOUT = timedelta(minutes=30)

#: A preview ends this long after it started, however busy (`max_length`).
MAX_LENGTH = timedelta(hours=2)

#: How finely a request in a preview moves its `last_active_at`: at most once
#: this often, so a busy preview is not one `UPDATE` per request (decision 311).
#: Idle is measured from it, so a preview may end up to this much early.
ACTIVITY_RESOLUTION = timedelta(minutes=1)

#: The two limits as the API's descriptions state them, derived so they cannot drift.
IDLE_WORDS = f"{IDLE_TIMEOUT // timedelta(minutes=1)} minutes"
MAX_LENGTH_WORDS = f"{MAX_LENGTH // timedelta(minutes=1)} minutes"


class Lapse(NamedTuple):
    """Why a live preview ended on its own, and the moment it did."""

    reason: PreviewEndReason
    at: datetime


def expires_at(started_at: datetime) -> datetime:
    """When a preview started at ``started_at`` ends at the latest."""
    return started_at + MAX_LENGTH


def lapse(*, started_at: datetime, last_active_at: datetime, now: datetime) -> Lapse | None:
    """**The one rule for a live preview that has run out**, or ``None`` while
    it holds.

    Idle once 30 minutes pass after its last request, and at its length once
    two hours pass after it started, each at the boundary itself. When both
    have passed, the one that came first is the reason, and its moment is when
    the preview ended: so a preview read long after it went idle says `idle`,
    at the time it did.
    """
    idle_at = last_active_at + IDLE_TIMEOUT
    max_at = expires_at(started_at)
    if idle_at < max_at:
        return Lapse(PreviewEndReason.IDLE, idle_at) if now >= idle_at else None
    return Lapse(PreviewEndReason.MAX_LENGTH, max_at) if now >= max_at else None
