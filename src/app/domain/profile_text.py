"""Length limits on the profile text a person writes, shared by every layer."""

from __future__ import annotations

__all__ = ["ABOUT_ME_MAX_LENGTH", "PROMPT_VERSION_MAX_LENGTH", "PROMPT_VERSION_PATTERN"]

#: The longest bio (`user_profiles.about_me`) a save accepts (owner,
#: 2026-10-17; decision 263). A bio stored before the limit is kept as it is.
ABOUT_ME_MAX_LENGTH = 1500

#: The shape of a bio's `about_me_prompt_version`: the AI router's
#: `PROMPT_VERSIONS` form, `<task>-v<n>` (`bio_draft-v1`; decision 266). Read by
#: the `user_profiles` CHECK, which a test pins to the database's.
PROMPT_VERSION_PATTERN = r"^[a-z][a-z0-9_]*-v[0-9]+$"
PROMPT_VERSION_MAX_LENGTH = 64
