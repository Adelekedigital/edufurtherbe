"""A missed session's reason, as the party who missed it gives it.

The note's bound lives here once: the request model and the column's `CHECK`
both read it, and a test pins the live constraint to it.
"""

from __future__ import annotations

#: The longest note a party may add to their reason, in characters.
NOTE_MAX_LENGTH = 1000
