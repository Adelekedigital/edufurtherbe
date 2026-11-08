"""Which admin grants may do what: each permission one named set (owner, 2026-11-07).

Built on the grant levels `AdminRole` already carries (decision 70). A route's
gate (`require_admin`) and a flag telling the caller whether they may use it
(`can_preview_as_user`, `can_reveal`) both read the same set through
`holds_any`, so the two cannot disagree. Narrowing a permission later is a
one-line change to its set.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.domain.enums import AdminRole

__all__ = ["EVERY_GRANT", "PREVIEW", "REVEAL", "holds_any"]

#: Every live grant: support work whoever is on shift may do (credits, the
#: queues, the Mentors page).
EVERY_GRANT: frozenset[AdminRole] = frozenset(AdminRole)

#: Previewing the app as a user, read only (decision 309).
PREVIEW: frozenset[AdminRole] = EVERY_GRANT

#: Revealing a previewed user's messages (decision 309).
REVEAL: frozenset[AdminRole] = EVERY_GRANT


def holds_any(held: Iterable[str], permitted: Iterable[AdminRole]) -> bool:
    """Whether a caller holding the live grants ``held`` may do what
    ``permitted`` allows. **`super_admin` always may**, without being listed in
    each set, as `require_admin` has always admitted it."""
    allowed = {AdminRole.SUPER_ADMIN.value, *(str(role) for role in permitted)}
    return not allowed.isdisjoint(str(role) for role in held)
