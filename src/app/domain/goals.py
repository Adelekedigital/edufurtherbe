"""Rules about a mentee's goal that more than one layer states."""

from __future__ import annotations

__all__ = ["FIELD_OF_STUDY_MAX_LENGTH"]

#: The longest field of study a goal holds, after composing (decision 259). The
#: API refuses longer and `ck_mentee_goals_field_of_study_length` backs it.
FIELD_OF_STUDY_MAX_LENGTH = 100
