"""What a user has seen: keys the frontend chooses, stored per user (decision 356).

The backend knows nothing about what a key means (a "what's new" tour, a
nudge, an announcement): it stores the key, when it was first seen, and gives
the keys back on `GET /me`. A new announcement is a new key, never a backend
change.

The bounds live here once: the column's `CHECK` reads them, the route's path
parameter reads them, and a migration test pins the live constraint to them.
"""

from __future__ import annotations

#: The longest key, in characters.
KEY_MAX_LENGTH = 80

#: A key: lower-case letters and digits, then any of `:._-`. ASCII only, so a
#: character is a byte and the bound is the same in both.
KEY_PATTERN = rf"^[a-z0-9][a-z0-9:._-]{{0,{KEY_MAX_LENGTH - 1}}}$"

#: How many keys one user keeps. Marking a new key past it drops the oldest,
#: so the store cannot grow without bound.
MAX_SEEN_ITEMS = 200
