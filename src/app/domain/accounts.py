"""Deleting your own account: the product's rules, without I/O.

Mentee Settings PRD (2026-10-19). The privacy policy promises deletion from
Settings within 30 days, and a person who changes their mind may ask support to
restore the account until then.
"""

from __future__ import annotations

import datetime as dt

from app.domain.enums import SessionReasonCode

#: How long a self-deleted account can still be restored before it is purged.
RESTORE_WINDOW = dt.timedelta(days=30)

#: The reason on the `unlisted` event a deleted mentor gets: the session reason's
#: own value, so the two cannot drift. Free text there, like an admin's, not an
#: `UnlistedReason`: that vocabulary is pinned to a dropped type.
#: Not `mentor_paused` either, so a restored mentor stays unlisted until an admin
#: lists them again.
UNLISTED_ON_DELETION = SessionReasonCode.ACCOUNT_DELETED.value
