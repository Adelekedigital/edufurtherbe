"""The admin surface: grants, institutions, mentor decisions, featured weeks."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any
from uuid import UUID

from fastapi import Body, Depends, Query
from pydantic import AwareDatetime

from app.api.deps.core import (
    ENDPOINT_ADMIN_CREDITS,
    GRANTED,
    CatalogueAdminDep,
    CreditAdminDep,
    IdempotencyKeyHeader,
    LadderDep,
    MentorAdminDep,
    OwnerDep,
    QueueViewerDep,
    SessionDep,
    claim_idempotency_key,
)
from app.api.schemas.admin import DeclineRequest, FeaturedWrite, MergeRequest
from app.api.schemas.admin_credits import AdminCreditGrantWrite
from app.api.schemas.common import (
    MAX_PAGE_SIZE,
    clamp_limit,
    decode_cursor,
    encode_cursor,
)
from app.api.schemas.profile import PauseRequest
from app.core.errors import (
    ConflictError,
    ValidationError,
)
from app.domain.enums import MentorStatusType
from app.domain.featured import week_start as week_of
from app.infra.db.admin_credits import grant_credits, list_admin_grants
from app.infra.db.admin_store import (
    approve_institution,
    merge_institution,
    pending_institutions,
    pending_mentors,
)
from app.infra.db.featured_store import (
    featured_schedule,
    remove_featured,
    set_featured,
)
from app.infra.db.idempotency import Replayed, record_response
from app.infra.db.mentor_status_store import (
    decide,
    history,
    may_self_resume,
    pause,
    resume,
    set_listing,
)

# `get_session` is aliased: this module already has one, and it is the **database
# session** dependency at line 142. Two callables with that name in one file is a
# collision a reader resolves by scrolling, and the wrong one is a plausible
# mistake rather than an obvious error — `bubble_id` shadowed a local the same
# way in the M4 transform and raised `UnboundLocalError` far from the edit.


def _canonical_grant(payload: AdminCreditGrantWrite) -> dict[str, Any]:
    """The request as the endpoint actually treats it.

    Recipients sorted and deduplicated, because `grant_credits` does both and
    says so in the OpenAPI description. The fingerprint has to agree with the
    behaviour, or a legitimate retry looks like a new request.
    """
    body = payload.model_dump(mode="json")
    body["user_ids"] = sorted({str(user_id) for user_id in payload.user_ids})
    return body


async def granted_admin_credits(
    payload: AdminCreditGrantWrite,
    admin_id: CreditAdminDep,
    session: SessionDep,
    ladder: LadderDep,
    idempotency_key: Annotated[str, IdempotencyKeyHeader],
) -> tuple[dict[str, Any], int, bool]:
    """Reserve the key, write the lots, store the answer — one transaction.

    **Required rather than optional, for the reason booking gives**: this is
    money, and an optional header makes the guarantee opt-in for exactly the
    caller who most needs it. A double-submitted grant is not recoverable by
    the admin noticing — the credits are already spendable.

    **The key and the write commit together**, so a stored `201` for lots that
    were never inserted cannot replay ids of nothing.
    """
    reservation = await claim_idempotency_key(
        session,
        key=idempotency_key,
        user_id=admin_id,
        endpoint=ENDPOINT_ADMIN_CREDITS,
        # **Canonicalised, because the endpoint promises duplicates and order
        # do not matter.** `request_fingerprint` sorts object keys but leaves
        # list order alone, so a retry that deduped or reordered its
        # recipients — which the description tells clients is harmless —
        # hashes differently and is refused as a *different* request. The
        # admin then has no way to learn whether the first attempt landed,
        # which is the one question a retry is asking.
        body=_canonical_grant(payload),
    )
    if isinstance(reservation, Replayed):
        return reservation.body, reservation.status_code, True

    result = await grant_credits(
        session,
        admin_id=admin_id,
        user_ids=tuple(payload.user_ids),
        quantity=payload.quantity,
        note=payload.note,
        ladder=ladder,
        now=dt.datetime.now(dt.UTC),
    )
    body = {
        "granted": [str(user_id) for user_id in result.granted],
        "unresolved": [str(user_id) for user_id in result.unresolved],
        "quantity": payload.quantity,
    }
    # The answer and the lots commit together, so a stored response cannot
    # survive a crash that lost the credits it describes.
    await record_response(session, reservation, status_code=GRANTED, body=body)
    await session.commit()
    return body, GRANTED, False


async def admin_grant_history(
    _: CreditAdminDep,
    session: SessionDep,
    # `le=MAX_PAGE_SIZE`, not the `le=200` the offset-paged admin queues use:
    # this one goes through `clamp_limit`, which caps at 50, so publishing 200
    # would advertise a bound the endpoint never satisfies.
    limit: Annotated[int | None, Query(ge=1, le=MAX_PAGE_SIZE)] = None,
    cursor: Annotated[str | None, Query(description="From a previous `next_cursor`.")] = None,
    granted_by: Annotated[
        uuid.UUID | None,
        Query(description="Narrow to one admin's grants."),
    ] = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """The history of admin credit grants, newest first.

    **Every admin sees every grant, including their own and each other's.** An
    audit only one person can read is not an audit — and the `granted_by` filter
    exists so somebody can narrow to their own without that being the default.

    Read by the same gate that writes: whoever may hand out credits may see what
    has been handed out, and splitting the two would let an admin create rows
    they cannot then review.
    """
    rows, has_more = await list_admin_grants(
        session,
        limit=clamp_limit(limit),
        after=decode_cursor(cursor),
        granted_by=granted_by,
    )
    # **Minted beside the decode**, the rule `mentor_page` states: issuing the
    # token where the sort key is known keeps the two halves of the codec from
    # drifting, which is the defect that once invalidated every cursor an
    # endpoint handed out.
    if not (has_more and rows):
        return rows, None
    last = rows[-1]
    return rows, encode_cursor(last["created_at"].isoformat(), last["id"])


AdminGrantHistoryDep = Annotated[
    tuple[list[dict[str, Any]], str | None], Depends(admin_grant_history)
]


async def pending_institution_rows(
    _: CatalogueAdminDep,
    session: SessionDep,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
) -> list[dict[str, Any]]:
    return await pending_institutions(session, limit=min(limit or 50, 200))


async def approved_institution(
    institution_id: UUID, _: CatalogueAdminDep, session: SessionDep
) -> bool:
    changed = await approve_institution(session, institution_id)
    await session.commit()
    return changed


async def merged_institution(
    institution_id: UUID, payload: MergeRequest, _: CatalogueAdminDep, session: SessionDep
) -> int:
    """The repoint and the retirement commit together, or neither does."""
    moved = await merge_institution(
        session, losing_id=institution_id, winning_id=payload.winning_id
    )
    await session.commit()
    return moved


async def pending_mentor_rows(
    _: QueueViewerDep, session: SessionDep, limit: Annotated[int | None, Query(ge=1, le=200)] = None
) -> list[dict[str, Any]]:
    return await pending_mentors(session, limit=min(limit or 50, 200))


async def decided_mentor(
    user_id: UUID,
    payload: DeclineRequest,
    admin_id: MentorAdminDep,
    session: SessionDep,
    approve: Annotated[bool, Query(description="True to approve, false to decline.")] = True,
) -> bool:
    changed = await decide(
        session, user_id=user_id, admin_id=admin_id, approved=approve, reason=payload.reason
    )
    await session.commit()
    return changed


async def featured_week_set(
    week_start: dt.date,
    payload: FeaturedWrite,
    admin_id: MentorAdminDep,
    session: SessionDep,
) -> dt.date:
    """An admin choosing a week's featured mentor (settled decision #188)."""
    await set_featured(
        session, week_start, payload.mentor_id, admin_id, now=dt.datetime.now(dt.UTC)
    )
    return week_start


async def featured_week_removed(
    week_start: dt.date, admin_id: MentorAdminDep, session: SessionDep
) -> bool:
    """An admin withdrawing their choice; the rotation resumes for that week."""
    del admin_id  # the gate is the point; who withdrew is not recorded
    return await remove_featured(session, week_start, now=dt.datetime.now(dt.UTC))


async def featured_weeks(
    admin_id: MentorAdminDep,
    session: SessionDep,
    since: Annotated[
        dt.date | None,
        Query(description="The earliest week to list. Default: eight weeks ago."),
    ] = None,
) -> list[dict[str, Any]]:
    """The featured schedule: past weeks and chosen future ones, newest first."""
    del admin_id
    start = since or week_of(dt.datetime.now(dt.UTC)) - dt.timedelta(weeks=8)
    return await featured_schedule(session, since=start)


FeaturedWeekSetDep = Annotated[dt.date, Depends(featured_week_set)]
FeaturedWeekRemovedDep = Annotated[bool, Depends(featured_week_removed)]
FeaturedWeeksDep = Annotated[list[dict[str, Any]], Depends(featured_weeks)]


async def listed_mentor(
    user_id: UUID,
    payload: DeclineRequest,
    admin_id: MentorAdminDep,
    session: SessionDep,
    listed: Annotated[bool, Query(description="True to list, false to unlist.")] = True,
) -> bool:
    """An admin moving a mentor's listing without touching their approval."""
    changed = await set_listing(
        session, user_id=user_id, admin_id=admin_id, listed=listed, reason=payload.reason
    )
    await session.commit()
    return changed


async def mentor_history(
    user_id: UUID,
    _: QueueViewerDep,
    session: SessionDep,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
    status: Annotated[
        list[MentorStatusType] | None,
        Query(description="Repeat to widen: `?status=approved&status=declined`."),
    ] = None,
    since: Annotated[
        AwareDatetime | None,
        Query(description="Only events at or after this instant. Inclusive."),
    ] = None,
    until: Annotated[
        AwareDatetime | None,
        Query(description="Only events strictly before this instant. Exclusive."),
    ] = None,
) -> list[dict[str, Any]]:
    """One mentor's transitions, narrowed.

    **The range is validated here rather than in the store**, unlike `/slots`.
    There, an omitted `start` means the mentor's today and the default is only
    knowable after the query that finds them, so legality could not be decided
    at the edge. Here both bounds are absolute instants a caller either sent or
    did not, and nothing downstream can change them — so the edge is where it
    belongs.
    """
    if since is not None and until is not None and until <= since:
        raise ValidationError("`until` must be after `since`")
    return await history(
        session,
        user_id,
        limit=min(limit or 50, 200),
        kinds=status or (),
        since=since,
        until=until,
    )


async def paused_self(
    user_id: OwnerDep,
    session: SessionDep,
    payload: Annotated[PauseRequest | None, Body()] = None,
) -> bool:
    """Refused while an admin's unlisting stands: pausing over it would make the
    newest unlisting the mentor's own, and their resume would then undo it (#75)."""
    outcome = await pause(
        session,
        user_id=user_id,
        return_on=payload.return_on if payload else None,
        now=dt.datetime.now(dt.UTC),
    )
    if outcome == "refused":
        raise ConflictError(
            "an admin has unlisted this profile; only an admin can change its listing"
        )
    await session.commit()
    return outcome == "paused"


async def resumed_self(user_id: OwnerDep, session: SessionDep) -> bool:
    """Refused unless this mentor was the one who paused themselves.

    Checked here rather than in the route because the answer needs the database:
    it is the newest unlisting's reason, and an admin's unlisting must not be
    undoable by the person it concerns.
    """
    if not await may_self_resume(session, user_id):
        return False
    resumed = await resume(session, user_id=user_id)
    await session.commit()
    return resumed


PendingInstitutionsDep = Annotated[list[dict[str, Any]], Depends(pending_institution_rows)]
ApprovedInstitutionDep = Annotated[bool, Depends(approved_institution)]
MergedInstitutionDep = Annotated[int, Depends(merged_institution)]
PendingMentorsDep = Annotated[list[dict[str, Any]], Depends(pending_mentor_rows)]
DecidedMentorDep = Annotated[bool, Depends(decided_mentor)]
ListedMentorDep = Annotated[bool, Depends(listed_mentor)]
MentorHistoryDep = Annotated[list[dict[str, Any]], Depends(mentor_history)]
PausedSelfDep = Annotated[bool, Depends(paused_self)]
ResumedSelfDep = Annotated[bool, Depends(resumed_self)]

AdminCreditGrantDep = Annotated[tuple[dict[str, Any], int, bool], Depends(granted_admin_credits)]
