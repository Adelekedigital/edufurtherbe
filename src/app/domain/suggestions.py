"""A mentor suggesting another time when they decline or cancel (#339).

Owner decisions, 2026-10-03 (decision 230):

- the original session **ends exactly as it would have** — declined or
  cancelled, refunded by decision 229 — and the suggestion is a separate offer;
- **one** suggested time, because each suggestion holds a slot;
- the slot is **held for two hours** for that mentee alone;
- the mentee is **reminded thirty minutes before the hold lapses**, if they have
  not booked it yet;
- booking it is an ordinary booking, which spends a credit as every booking does.

Pure: these are the numbers and the clock arithmetic, and nothing else.
"""

from __future__ import annotations

import datetime as dt

__all__ = [
    "SUGGESTION_HOLD",
    "SUGGESTION_REMINDER_KIND",
    "SUGGESTION_REMINDER_LEAD",
    "held_until",
    "suggestion_reminder_at",
]

#: How long a suggested slot is kept for the mentee it was offered to.
SUGGESTION_HOLD = dt.timedelta(hours=2)

#: How long before the hold lapses the mentee is nudged.
SUGGESTION_REMINDER_LEAD = dt.timedelta(minutes=30)

#: What the reminder callback carries and the outbox dedups on. Prefixed `h`
#: (hold) for the reason `s`, `t`, `r` and `c` are: several kinds of reminder
#: share one column, and a bare number would let one rule fire another's.
SUGGESTION_REMINDER_KIND = "h30"


def held_until(now: dt.datetime) -> dt.datetime:
    """When a suggestion made at ``now`` stops holding its slot."""
    return now + SUGGESTION_HOLD


def suggestion_reminder_at(until: dt.datetime) -> dt.datetime:
    """When the mentee is nudged about a hold that lapses at ``until``."""
    return until - SUGGESTION_REMINDER_LEAD
