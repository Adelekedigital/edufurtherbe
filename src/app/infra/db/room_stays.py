"""Who is in a session's Daily room **now** (#394): the one writer and the one
reader of ``session_room_stays``.

A stay is one Daily join of one party, keyed on the participant and Daily's
per-join id. ``participant.joined`` opens it and ``participant.left`` closes
it; either may arrive first or twice, and the pair ends the same: a join never
reopens a stay a leave already closed.

Matched on the session's own room **and** a party's user id, together, the
rule a sighting is matched by (`observe_presence`), so an event for anybody
else, or for a party of another session, writes nothing.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import TIMESTAMP, and_, case, func, literal, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.attendance import DOOR_STATUSES
from app.infra.db.models.sessions import Session, SessionParticipant, SessionRoomStay
from app.infra.db.predicates import presence_decides_sql, session_ends

COLUMNS = ["participant_id", "provider_session_id", "joined_at", "left_at"]


def _the_party(
    room: str, user_id: UUID, stay: str, joined: dt.datetime, left: dt.datetime | None
) -> Any:
    """The stay's row, for the party of the session with this room, or none."""
    return select(
        SessionParticipant.id,
        literal(stay),
        literal(joined, TIMESTAMP(timezone=True)),
        literal(left, TIMESTAMP(timezone=True)),
    ).where(
        SessionParticipant.session_id == Session.id,
        SessionParticipant.user_id == user_id,
        Session.external_room_id == room,
        presence_decides_sql(),
    )


async def open_stay(
    session: AsyncSession, *, room: str, user_id: UUID, stay: str, joined: dt.datetime
) -> None:
    """Record a join. A repeat, or a join whose leave already arrived, changes
    nothing. Does not commit."""
    await session.execute(
        insert(SessionRoomStay)
        .from_select(COLUMNS, _the_party(room, user_id, stay, joined, None))
        .on_conflict_do_nothing(index_elements=["participant_id", "provider_session_id"])
    )


async def close_stay(
    session: AsyncSession,
    *,
    room: str,
    user_id: UUID,
    stay: str,
    joined: dt.datetime,
    left: dt.datetime,
) -> None:
    """Record a leave, opening and closing the stay at once when its join has
    not arrived. Does not commit.

    Never before the stored join: the join's own delivery and the leave carry
    the same ``joined_at``, but the later of the two is kept so the row's
    ``CHECK`` holds whatever Daily sends.
    """
    statement = insert(SessionRoomStay).from_select(
        COLUMNS, _the_party(room, user_id, stay, joined, left)
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["participant_id", "provider_session_id"],
            set_={"left_at": func.greatest(SessionRoomStay.joined_at, statement.excluded.left_at)},
        )
    )


def in_room(side: Any, label: str, *, party_left: Any) -> Any:
    """Whether the party in ``side`` (``Session.mentor_id`` or ``mentee_id``) is
    in the room now, correlated to ``Session``.

    ``NULL`` where presence is not reported (Meet, custom, no room), because
    "unknown" is not "absent", and for a party whose account is gone
    (``party_left``, the reader's ``live()`` join), who takes their presence
    with the rest of their details (decision 212).

    Otherwise true while one of their stays is open, **the session is one the
    room is open for** (``DOOR_STATUSES``: not cancelled, declined, withdrawn
    or expired) **and it has not ended**: the room closes then
    (``door_window``), and a leave whose delivery was lost (Daily stops after
    three failures) must not leave somebody in the room forever. The two cheap
    tests come first, so an ended session never reaches the subquery. Compared
    on the database's clock, the only one in a read.
    """
    open_stay_exists = (
        select(SessionRoomStay.id)
        .join(SessionParticipant, SessionParticipant.id == SessionRoomStay.participant_id)
        .where(
            SessionParticipant.session_id == Session.id,
            SessionParticipant.user_id == side,
            SessionRoomStay.left_at.is_(None),
        )
        .correlate(Session)
        .exists()
    )
    return case(
        (party_left, None),
        (
            presence_decides_sql(),
            and_(
                Session.status.in_([str(s) for s in DOOR_STATUSES]),
                func.now() < session_ends(),
                open_stay_exists,
            ),
        ),
        else_=None,
    ).label(label)
