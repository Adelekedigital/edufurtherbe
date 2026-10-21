"""How many similar mentors a profile suggests (decisions 186, 211).

One constant, here rather than in the store, so the store's limit, the
route's published description and the tests all read the same number.
"""

#: The owner's Mentor Profile PRD (2026-10-21): "3 to 5". Fewer come back only
#: when fewer listed mentors share an offering with the profile.
SIMILAR_LIMIT = 5
