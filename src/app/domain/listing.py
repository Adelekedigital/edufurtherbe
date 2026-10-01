"""Rules about a mentor taking themselves off the listing and coming back.

Pure: the caller supplies the mentor's own today, because "today" for a return
date is the date in *their* zone, which only the data layer can know.
"""

from __future__ import annotations

import datetime as dt

__all__ = ["RETURN_ON_POINTER", "return_on_problem"]

#: Where a refused return date is reported in the pause request.
RETURN_ON_POINTER = "/return_on"


def return_on_problem(return_on: dt.date | None, today: dt.date) -> str | None:
    """Why this return date cannot be set, or ``None`` if it can.

    **After the mentor's today, never on it.** The reminder goes out on the
    morning of the date, so a date that is today or past would be due at once:
    a pause that reminds the mentor to come back before they have left. Null is
    "not sure yet" and is always allowed.
    """
    if return_on is not None and return_on <= today:
        return "return_on must be after today in your time zone"
    return None
