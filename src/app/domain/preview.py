"""An admin's read-only preview of the app as one user sees it (decision 309).

The bounds live here once: the request models and the columns' `CHECK`s both
read them, and a migration test pins the live constraints to them.
"""

from __future__ import annotations

#: The longest page path a preview logs, in characters.
PAGE_PATH_MAX_LENGTH = 2048

#: The longest reason an admin may give for a reveal, in characters.
REVEAL_REASON_MAX_LENGTH = 500
