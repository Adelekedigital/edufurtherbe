"""An admin's internal note on a mentor (Admin PRD, 2026-10-26).

The body's bound lives here once: the model's `CHECK` reads it (the request
model will, when the notes endpoint lands), and a test pins the live
constraint to it.
"""

from __future__ import annotations

#: The longest note an admin may leave, in characters.
NOTE_BODY_MAX_LENGTH = 2000
