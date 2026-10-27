"""The credit ledger as an admin reads it, and the mentees whose credits they manage.

**One entry builder for both readers** (decision 299, non-negotiable #8): a
mentee's history is the platform log narrowed to that mentee, so an entry cannot
read one way on the panel and another in the log.

Like `admin_store`, this reads other people's rows on purpose: the caller's
grant is checked by `api.deps.require_admin` before anything here runs.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, case, exists, func, literal, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.domain.credit_log import (
    FILTER_KINDS,
    GRANT_KINDS,
    REASON_KINDS,
    CreditEntryKind,
)
from app.domain.enums import CreditReason
from app.infra.db import keyset
from app.infra.db.catalogue_store import LIKE_ESCAPE, escape_like
from app.infra.db.models.credits import AdminCreditGrant, CreditLot, CreditTransaction
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.sessions import Session
from app.infra.db.models.user import User, UserProfile
from app.infra.db.predicates import LIVE, live
from app.infra.db.session_stats import BY_MENTEE, BY_MENTOR
from app.infra.db.session_store import cancelled_by

__all__ = [
    "MENTEE_SEARCH_LIMIT",
    "RECENT_MENTEES",
    "credit_entries",
    "find_mentees",
    "get_mentee",
    "is_mentee",
]

#: How many mentees a search returns.
MENTEE_SEARCH_LIMIT = 20
#: How many mentees an empty search returns: those with the latest credit
#: activity (PRD Q3).
RECENT_MENTEES = 10


def is_mentee() -> Any:
    """A live user with no live mentor profile: who holds credits on the admin
    Credits page. Correlated on `User`; scoped in the statement (non-negotiable
    #5), so a mentor's id answers nothing."""
    return and_(
        LIVE,
        not_(
            exists().where(
                MentorProfile.user_id == User.id,
                MentorProfile.deleted_at.is_(None),
            )
        ),
    )


_MENTEE_COLUMNS = (
    User.id.label("user_id"),
    User.first_name,
    User.last_name,
    User.email,
    UserProfile.avatar_url,
    UserProfile.cover_color,
)


def _mentees() -> Select[Any]:
    return (
        select(*_MENTEE_COLUMNS)
        .select_from(User)
        .outerjoin(UserProfile, UserProfile.user_id == User.id)
        .where(is_mentee())
    )


def _as_uuid(term: str) -> UUID | None:
    try:
        return UUID(term)
    except ValueError:
        return None


async def find_mentees(session: AsyncSession, query: str | None) -> list[dict[str, Any]]:
    """Up to `MENTEE_SEARCH_LIMIT` mentees matching ``query``, or with none, the
    `RECENT_MENTEES` with the latest credit activity.

    A full user id matches that user only. Anything else is a case-insensitive
    substring of the email, the first name, the last name or the two joined,
    with `LIKE` wildcards escaped. Ordered by name, then id, so a page is stable.
    """
    if not query:
        latest = (
            select(func.max(CreditTransaction.created_at))
            .where(CreditTransaction.user_id == User.id)
            .correlate(User)
            .scalar_subquery()
        )
        statement = (
            _mentees()
            .where(latest.is_not(None))
            .order_by(latest.desc(), User.id.desc())
            .limit(RECENT_MENTEES)
        )
    else:
        user_id = _as_uuid(query)
        if user_id is not None:
            match: Any = User.id == user_id
        else:
            pattern = "%" + escape_like(query) + "%"
            full_name = func.concat_ws(" ", User.first_name, User.last_name)
            match = or_(
                *(
                    column.ilike(pattern, escape=LIKE_ESCAPE)
                    for column in (User.email, User.first_name, User.last_name, full_name)
                )
            )
        statement = (
            _mentees()
            .where(match)
            .order_by(User.first_name, User.last_name, User.id)
            .limit(MENTEE_SEARCH_LIMIT)
        )
    return [dict(row) for row in (await session.execute(statement)).mappings()]


async def get_mentee(session: AsyncSession, user_id: UUID) -> dict[str, Any] | None:
    """One mentee by id, or ``None`` for nobody, a deleted user or a mentor."""
    row = (await session.execute(_mentees().where(User.id == user_id))).mappings().first()
    return None if row is None else dict(row)


def _kind() -> Any:
    """`domain.credit_log`'s table as SQL: the reason's kind, or for a grant its
    lot's source's."""
    return case(
        *(
            (CreditTransaction.reason == reason, literal(kind))
            for reason, kind in REASON_KINDS.items()
        ),
        *(
            (
                and_(CreditTransaction.reason == CreditReason.GRANT, CreditLot.source == source),
                literal(kind),
            )
            for source, kind in GRANT_KINDS.items()
        ),
    )


def _refund_reason() -> Any:
    """Why a refund was paid; null for anything else. A cancellation is told
    apart by `session_store.cancelled_by`, the rule the session read uses."""
    by = cancelled_by()
    return case(
        (CreditTransaction.reason == CreditReason.SESSION_NO_SHOW_REFUND, "mentor_no_show"),
        (CreditTransaction.reason == CreditReason.REQUEST_UNFULFILLED, "request_unfulfilled"),
        (
            CreditTransaction.reason == CreditReason.SESSION_CANCELLED_REFUND,
            case(
                (by == BY_MENTOR, "mentor_canceled"),
                (by == BY_MENTEE, "mentee_canceled"),
                else_="canceled",
            ),
        ),
        else_=None,
    )


def _balance_after() -> Any:
    """The mentee's ledger total up to and including this row, in list order.

    The ledger's own running total: it equals the card's balance except for a
    credit past its date the expiry sweep has not recorded yet. Correlated on
    `ix_credit_transactions_user_created`, so it reads one person's ledger.
    """
    earlier = aliased(CreditTransaction)
    return (
        select(func.coalesce(func.sum(earlier.delta), 0))
        .where(
            earlier.user_id == CreditTransaction.user_id,
            or_(
                earlier.created_at < CreditTransaction.created_at,
                and_(
                    earlier.created_at == CreditTransaction.created_at,
                    earlier.id <= CreditTransaction.id,
                ),
            ),
        )
        .correlate(CreditTransaction)
        .scalar_subquery()
    )


async def credit_entries(
    session: AsyncSession,
    *,
    limit: int,
    after: tuple[str, UUID] | None = None,
    kinds: Sequence[CreditEntryKind] = FILTER_KINDS["all"],
    mentee_id: UUID | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """One page of ledger entries, newest first, keyset on `(created_at, id)`.

    Every movement of every balance, or one mentee's with ``mentee_id``, narrowed
    to ``kinds``. Names come through `live()`, so a person who deleted their
    account reads as unnamed (decision 212) while the row stays. The admin and
    the note are a grant's own (`admin_credit_grants`), never an expiry's on the
    same lot.
    """
    mentee = aliased(User)
    mentor = aliased(User)
    admin = aliased(User)
    kind = _kind()
    statement = (
        select(
            CreditTransaction.id,
            CreditTransaction.created_at,
            CreditTransaction.delta,
            kind.label("kind"),
            _refund_reason().label("refund_reason"),
            _balance_after().label("balance_after"),
            CreditTransaction.user_id.label("mentee_id"),
            mentee.first_name.label("mentee_first_name"),
            mentee.last_name.label("mentee_last_name"),
            mentor.first_name.label("mentor_first_name"),
            mentor.last_name.label("mentor_last_name"),
            CreditTransaction.session_id,
            admin.first_name.label("admin_first_name"),
            admin.last_name.label("admin_last_name"),
            AdminCreditGrant.granted_by.label("admin_id"),
            AdminCreditGrant.note,
        )
        .select_from(CreditTransaction)
        .join(CreditLot, CreditLot.id == CreditTransaction.credit_lot_id)
        .outerjoin(mentee, and_(mentee.id == CreditTransaction.user_id, live(mentee)))
        .outerjoin(Session, Session.id == CreditTransaction.session_id)
        .outerjoin(mentor, and_(mentor.id == Session.mentor_id, live(mentor)))
        .outerjoin(
            AdminCreditGrant,
            and_(
                AdminCreditGrant.credit_lot_id == CreditTransaction.credit_lot_id,
                CreditTransaction.reason == CreditReason.GRANT,
            ),
        )
        .outerjoin(admin, and_(admin.id == AdminCreditGrant.granted_by, live(admin)))
        .where(kind.in_(tuple(kinds)))
    )
    if mentee_id is not None:
        statement = statement.where(CreditTransaction.user_id == mentee_id)
    if after is not None:
        statement = statement.where(
            keyset.after(CreditTransaction.created_at, CreditTransaction.id, after)
        )
    statement = statement.order_by(
        CreditTransaction.created_at.desc(), CreditTransaction.id.desc()
    ).limit(limit + 1)
    rows = [dict(row) for row in (await session.execute(statement)).mappings()]
    return rows[:limit], len(rows) > limit
