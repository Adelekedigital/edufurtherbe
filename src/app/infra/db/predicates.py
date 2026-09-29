"""Row-visibility predicates, defined once and imported by every store.

``LIVE`` began in ``provisioning_store.py`` as a single expression object, put
there after the same rule had been hand-typed into five statements and missed on
the fifth — the ``UPDATE``, so a user soft-deleted mid-run would have been handed
a live Supabase account.

This module exists because a second store now needs it, which is exactly the
extract-on-the-second-occurrence case non-negotiable #8 names. Every statement
that reads or writes an existing ``users`` row composes ``LIVE``; the parity test
in ``tests/unit/test_predicates.py`` walks each store's declared statements and
fails any that omits it.
"""

from __future__ import annotations

from typing import Any

from app.infra.db.models.user import User


def live(user: Any) -> Any:
    """``LIVE`` for an aliased ``users`` — a statement joining two people.

    One definition: ``LIVE`` below is this applied to ``User`` itself, so the
    rule cannot mean one thing for the mentor alias and another everywhere else.
    """
    return user.deleted_at.is_(None)


#: "This user still exists." An expression object rather than a string, so a
#: statement that omits it is missing a *name* — something a reader and a test
#: can both see — rather than missing a substring nobody notices.
LIVE = live(User)
