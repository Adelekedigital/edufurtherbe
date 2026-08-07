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

from app.infra.db.models.user import User

#: "This user still exists." An expression object rather than a string, so a
#: statement that omits it is missing a *name* — something a reader and a test
#: can both see — rather than missing a substring nobody notices.
LIVE = User.deleted_at.is_(None)
