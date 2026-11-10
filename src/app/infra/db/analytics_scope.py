"""Who the admin Analytics numbers leave out, said once (PRD 31, decision 324).

Every analytics statement composes `counted_user` or `counted_session`, so the
tabs cannot disagree about who is excluded. Left out: anybody holding a live
admin grant (`is_live_admin`) and the dev demo accounts (`DEMO_DOMAIN`).
Preview sessions need nothing here: a preview writes nothing (decision 313).
PRD 30's `is_internal` joins `_excluded_users` when it ships, and nowhere else.

A deleted account is **counted**: what it did happened.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select

from app.infra.db.demo_seed import DEMO_DOMAIN
from app.infra.db.models.sessions import Session
from app.infra.db.models.user import User
from app.infra.db.predicates import is_live_admin


def _excluded_users() -> Any:
    """The ids no analytics figure counts."""
    return select(User.id).where(or_(User.email.like("%@" + DEMO_DOMAIN), is_live_admin(User.id)))


def counted_user(user_id: Any) -> Any:
    """``user_id`` is somebody the analytics count. `users.id` is never null,
    so `NOT IN` is safe here."""
    return user_id.not_in(_excluded_users())


def counted_session() -> Any:
    """A session both of whose people are counted."""
    excluded = _excluded_users()
    return Session.mentor_id.not_in(excluded) & Session.mentee_id.not_in(excluded)
