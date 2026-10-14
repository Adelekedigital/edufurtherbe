"""Every scheduled-callback kind is distinct (decision 245).

The reminder callback dispatches on `kind`, checking the session reminders
first. A new kind reusing another's value, or the expiry's `x` prefix, would
send that callback down the wrong branch with nothing to say so.
"""

from app.domain.notifications import REMINDER_OFFSETS, SESSION_REMINDER_KINDS
from app.domain.sessions import EXPIRY_KIND
from app.domain.suggestions import SUGGESTION_REMINDER_KIND


def test_the_expiry_kind_is_no_reminders_kind() -> None:
    reminders = {*REMINDER_OFFSETS, *SESSION_REMINDER_KINDS, SUGGESTION_REMINDER_KIND}

    assert EXPIRY_KIND not in reminders
    assert not any(kind.startswith(EXPIRY_KIND[0]) for kind in reminders)


def test_the_reminder_kinds_do_not_overlap_each_other() -> None:
    groups = [set(REMINDER_OFFSETS), set(SESSION_REMINDER_KINDS), {SUGGESTION_REMINDER_KIND}]

    assert sum(len(g) for g in groups) == len(set().union(*groups))
