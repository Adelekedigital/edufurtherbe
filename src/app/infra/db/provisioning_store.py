"""Every database statement provisioning needs, in one place.

These lived in ``scripts/provision_auth.py``. Two reasons they are here instead,
and the second is the one that decides it.

**The gates cannot see ``scripts/``.** ``scripts/check.py`` runs ``mypy src`` and
``bandit -r src``, and coverage is measured over ``src/app``. Statements written
there are checked by ruff alone, and the integration tests exercising them count
for nothing against the coverage floor — delete the test file and the gate still
reports green.

**One rule, one representation.** ``deleted_at IS NULL`` is the whole of "a
soft-deleted user is invisible", and it was typed by hand into four statements
here and one in ``api/deps.py``. Four out of five is not a style problem: the
``UPDATE`` was the one that got missed, so a user soft-deleted mid-run would have
been handed a live account. It is now ``LIVE``, and ``test_provisioning_store``
walks ``USER_STATEMENTS`` asserting no statement touching ``users`` omits it.

**Built with SQLAlchemy Core rather than ``text()``, and that is a departure.**
``etl/loader.py``, ``etl/satellites.py`` and ``api/deps.py`` all write raw SQL,
which is right for a hand-written statement nobody composes. This module has to
compose: the whole point is one predicate reused across four statements. Doing
that with an f-string builds SQL by string concatenation, which ruff's `S608`
flags as an injection vector — correctly, in general, even though this particular
interpolation is a module constant. Suppressing a security rule to keep a
stylistic convention is the wrong trade, and a `WHERE` clause that is a real
expression object is a stronger form of "one representation" than a substring
that happens to be pasted in the right places.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import bindparam, insert, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.enums import AdminRole, PrimaryRole
from app.domain.provisioning import Candidate
from app.infra.db.models.admin import AdminUser
from app.infra.db.models.user import User
from app.infra.db.predicates import LIVE
from app.infra.db.triggers import timestamps_from_source

_IDENTITY = (User.id, User.email, User.auth_id)

CANDIDATES = select(*_IDENTITY).where(LIVE).order_by(User.id)

LINKED = select(*_IDENTITY).where(User.auth_id.is_not(None), LIVE).order_by(User.id)

BY_EMAIL = select(*_IDENTITY).where(User.email == bindparam("email"), LIVE)

# `LIVE` for the same reason `BY_EMAIL` carries it: a soft-deleted user still
# holds their `auth_id`, so a lookup without it would answer for somebody the
# rest of the system treats as gone.
BY_AUTH_ID = select(*_IDENTITY).where(User.auth_id == bindparam("auth_id"), LIVE)

# `auth_id IS NULL` makes a second concurrent run a no-op on rows the first
# already linked, rather than overwriting one Supabase identifier with another —
# which would orphan an account nothing can reach.
#
# `LIVE` is the same predicate `CANDIDATES` carries, and it is not a millisecond
# window: the candidate list is read once and the run then takes minutes to
# hours, so a user soft-deleted while it is in flight would otherwise be handed a
# live account from a plan built when they were live.
LINK = (
    update(User)
    .where(User.id == bindparam("user_id"), User.auth_id.is_(None), LIVE)
    .values(auth_id=bindparam("new_auth_id"))
)

INSERT_USER = (
    insert(User)
    .values(
        email=bindparam("email"),
        first_name=bindparam("first_name"),
        primary_role=bindparam("primary_role"),
        auth_id=bindparam("new_auth_id"),
    )
    .returning(User.id)
)

# The partial unique index `ix_admin_users_active_grant` is the conflict target —
# columns and predicate both — so re-granting a role someone already holds is a
# no-op while a *revoked* historical row does not block the re-grant.
#
# `granted_by` stays null: a CLI run has no acting user, and a synthetic one
# would look like knowledge we do not have, in the one table whose entire purpose
# is being auditable.
GRANT = (
    postgresql_insert(AdminUser)
    .values(user_id=bindparam("user_id"), admin_role=bindparam("admin_role"))
    .on_conflict_do_nothing(
        index_elements=[AdminUser.user_id, AdminUser.admin_role],
        index_where=AdminUser.revoked_at.is_(None),
    )
    .returning(AdminUser.id)
)

#: Every statement above that reads or writes ``users``. The parity test walks
#: this tuple rather than the module namespace, so adding a statement and
#: forgetting to list it shows up in a diff instead of silently going unchecked.
USER_STATEMENTS = (CANDIDATES, LINKED, BY_EMAIL, BY_AUTH_ID, LINK)


class ProvisioningStore:
    """The database side of provisioning. No HTTP, no policy, no printing.

    Takes an engine rather than a connection: each method opens the transaction
    it needs. That is what lets ``link`` commit per user, which is what makes a
    partially completed run resumable — work already done stays done.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def candidates(self) -> list[Candidate]:
        """Every live user, as provisioning sees them."""
        async with self._engine.connect() as connection:
            rows = (await connection.execute(CANDIDATES)).all()
        return [Candidate(user_id=row[0], email=row[1], auth_id=row[2]) for row in rows]

    async def linked(self) -> list[tuple[UUID, str, UUID]]:
        """Every live user that holds an ``auth_id``, for ``--verify``."""
        async with self._engine.connect() as connection:
            rows = (await connection.execute(LINKED)).all()
        return [(row[0], row[1], row[2]) for row in rows]

    async def by_email(self, email: str) -> Candidate | None:
        """One live user by address, or ``None``."""
        async with self._engine.connect() as connection:
            row = (await connection.execute(BY_EMAIL, {"email": email})).first()
        if row is None:
            return None
        return Candidate(user_id=row[0], email=row[1], auth_id=row[2])

    async def by_auth_id(self, auth_id: UUID) -> Candidate | None:
        """One live user by Supabase identifier, or ``None``."""
        async with self._engine.connect() as connection:
            row = (await connection.execute(BY_AUTH_ID, {"auth_id": auth_id})).first()
        if row is None:
            return None
        return Candidate(user_id=row[0], email=row[1], auth_id=row[2])

    async def link(self, user_id: UUID, auth_id: UUID) -> bool:
        """Record the link, preserving Bubble's ``updated_at``.

        **The trigger has to be held off.** ``updated_at`` carries Bubble's
        Modified Date for every migrated row — that is what M1c went to some
        trouble to load — and ``trg_set_updated_at`` fires on any ``UPDATE``.
        Without the disable, a provisioning run silently rewrites 1,200
        modification dates to the moment provisioning happened, one day after the
        migration that preserved them, with nothing to restore them from.

        Linking an auth identifier is not a modification of the user's data, so
        the honest value is the one already there.

        Returns ``False`` when the row would not take it — already linked by
        another run, or soft-deleted since the candidate list was read. The
        caller decides what that means; it is not always benign.
        """
        async with self._engine.begin() as connection:
            async with timestamps_from_source(connection, "users"):
                result = await connection.execute(
                    LINK, {"user_id": user_id, "new_auth_id": auth_id}
                )
            return result.rowcount > 0

    async def insert_user(
        self, *, email: str, first_name: str | None, role: PrimaryRole, auth_id: UUID
    ) -> UUID:
        """Create a new user row. Raises if the address or ``auth_id`` collides."""
        async with self._engine.begin() as connection:
            result = await connection.execute(
                INSERT_USER,
                {
                    "email": email,
                    "first_name": first_name,
                    # A StrEnum is already its own wire value, but passing the
                    # member would send the *name* on some drivers — the defect
                    # the deleted `pg_enum` helper existed to prevent one layer down.
                    "primary_role": role.value,
                    "new_auth_id": auth_id,
                },
            )
            return result.scalar_one()

    async def grant_admin(self, user_id: UUID, role: AdminRole) -> bool:
        """Grant a role. ``False`` when it was already held."""
        async with self._engine.begin() as connection:
            granted = (
                await connection.execute(GRANT, {"user_id": user_id, "admin_role": role.value})
            ).first()
        return granted is not None
