"""Rules about a mentee's goal that more than one layer states."""

from __future__ import annotations

__all__ = [
    "FIELD_OF_STUDY_MAX_LENGTH",
    "FIELD_OF_STUDY_SUGGESTIONS",
    "MAX_GOAL_SCHOOLS",
    "SCHOOL_NAME_MAX_LENGTH",
]

#: The longest field of study a goal holds, after composing (decision 259). The
#: API refuses longer and `ck_mentee_goals_field_of_study_length` backs it.
FIELD_OF_STUDY_MAX_LENGTH = 100

#: The most schools a goal names (Mentee Settings PRD). The API refuses an
#: eleventh; `ck_mentee_goal_schools_position_within_cap` backs it.
MAX_GOAL_SCHOOLS = 10

#: The longest school name a goal holds, as typed. The API refuses longer and
#: `ck_mentee_goal_schools_school_name_raw_length` backs it.
SCHOOL_NAME_MAX_LENGTH = 120

#: What onboarding offers under "What do you want to study?", in this order
#: (owner, 2026-10-17; decision 262). Suggestions only: a typed answer is saved
#: as typed, so nothing validates `field_of_study` against this list.
FIELD_OF_STUDY_SUGGESTIONS: tuple[str, ...] = (
    "Business and Management",
    "Economics and Econometrics",
    "Law",
    "Government and Political Science",
    "Computer and Information Science",
    "Mathematics and Statistics",
    "Engineering",
    "Physical Sciences",
    "Biological and Biomedical Sciences",
    "Health Science",
    "Psychology",
    "Social Sciences and History",
    "Education",
    "Communication and Journalism",
    "Visual and Performing Arts",
    "Public Health",
    "Data Science",
    "Arts and Humanities",
    "Architecture and Urban Planning",
    "Agriculture and Environmental Science",
)
