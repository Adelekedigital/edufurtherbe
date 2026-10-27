"""An admin's internal note on a mentor (Admin PRD, 2026-10-26).

The body's bound lives here once: the request model and the column's `CHECK`
both read it, and a test pins the live constraint to it.
"""

from __future__ import annotations

#: The longest note an admin may leave, in characters.
NOTE_BODY_MAX_LENGTH = 2000
