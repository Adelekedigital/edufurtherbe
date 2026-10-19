"""The countries a mentor can guide mentees towards (decision 276).

A mentor names up to eight, in their own order. Matching ignores them for now,
and the public profile page shows them only once a design places them; the API
returns them on the owner's and the public reads.
"""

__all__ = ["MAX_GUIDE_COUNTRIES"]

#: The most a mentor may name. The schema holds it structurally: `position` is
#: unique per mentor and below this, so a ninth row has nowhere to go.
MAX_GUIDE_COUNTRIES = 8
