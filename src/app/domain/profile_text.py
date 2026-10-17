"""Length limits on the profile text a person writes, shared by every layer."""

from __future__ import annotations

__all__ = ["ABOUT_ME_MAX_LENGTH"]

#: The longest bio (`user_profiles.about_me`) a save accepts (owner,
#: 2026-10-17; decision 263). A bio stored before the limit is kept as it is.
ABOUT_ME_MAX_LENGTH = 1500
