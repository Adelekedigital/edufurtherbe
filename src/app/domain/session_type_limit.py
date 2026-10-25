"""How many session types a mentor may hold (Session Types PRD, founder 2026-10-21).

Every offering not truly deleted counts: live, hidden and scheduled for
deletion alike, since each still sits on the mentor's list and may come back.
Support may raise or lower one mentor's limit; null means this default.
Lowering it below what a mentor holds deletes nothing and only blocks new
creates.
"""

from __future__ import annotations

__all__ = ["DEFAULT_SESSION_TYPE_LIMIT", "SESSION_TYPE_LIMIT_CEILING"]

#: The limit for a mentor whose `mentor_profiles.session_type_limit` is null.
DEFAULT_SESSION_TYPE_LIMIT = 5

#: The highest limit support may set: a sanity bound, not a product rule. The
#: column's `CHECK` holds the same number, pinned by its migration test.
SESSION_TYPE_LIMIT_CEILING = 100
