"""Writing a user's education, and the institution a new entry may need.

**Every statement here is already scoped.** The caller passes a `user_id` that
`api.deps.get_owner` resolved — a row it could not find is a caller who may not
write it — and every `UPDATE` and `DELETE` below repeats the scope in its own
`WHERE` anyway. That is not belt-and-braces for its own sake: the dependency
guards the URL, and the statement guards the row, and this project has already
shipped a defect where only the first of those was true.

CREATE-ON-WRITE, IN ONE TRANSACTION
===================================
A user types a school the catalogue does not hold. There is no separate endpoint
to create an institution — the row is created here, inside the same transaction
as the education entry, with ``created_by`` set from the dependency. So an
anonymous caller can never reach ``institutions`` (search is public, this is
not), and a failure leaves neither row rather than an orphaned institution
sitting in the review queue with nothing pointing at it.

**An ambiguous name is queued, never linked.** ``City University`` is a real
university in the United States, in Bangladesh and in the United Kingdom;
``domain.institutions.match`` returns ``None`` for all three rather than pick.
Linking one would file the degree in the wrong country, silently and
permanently, and the country of study derives from it.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.institutions import index_names, match
from app.infra.db.models.education import EducationEntry, Institution

#: Columns a client may set. `user_id`, `institution_id`, `deleted_at` and the
#: legacy anchor are decided here or by the dependency, never by the body.
WRITABLE = (
    "school_name_raw",
    "degree_level_id",
    "degree_category",
    "study_course",
    "study_program",
    "date_start",
    "date_end",
)


async def _resolve_institution(
    session: AsyncSession, *, school_name_raw: str, supplied: UUID | None, user_id: UUID
) -> tuple[UUID | None, bool]:
    """The institution this entry points at, and whether it had to be created.

    A supplied id is **validated, not trusted**: it must be a row a user could
    have selected, or one this same user created and is still awaiting review.
    Otherwise a caller could attach their degree to any row in the catalogue by
    id — including one merged away or withdrawn from search.
    """
    if supplied is not None:
        allowed = await session.execute(
            select(Institution.id).where(
                Institution.id == supplied,
                Institution.merged_into_id.is_(None),
                # Approved, or this caller's own pending row: they created it a
                # moment ago and it is not visible in search yet.
                (Institution.status == "approved") | (Institution.created_by == user_id),
            )
        )
        if allowed.first() is None:
            return None, False
        return supplied, False

    # No id: match by name, exactly or case-folded, never fuzzily.
    rows = await session.execute(select(Institution.name, Institution.id))
    matched = match(school_name_raw, index_names((row.name, row.id) for row in rows))
    if matched is not None:
        return matched, False

    created = await session.execute(
        insert(Institution)
        .values(
            name=school_name_raw,
            source="manual",
            status="pending_review",
            created_by=user_id,
        )
        .returning(Institution.id)
    )
    return created.scalar_one(), True


async def _clear_most_recent(
    session: AsyncSession, user_id: UUID, *, except_id: UUID | None = None
) -> None:
    """Take the flag off the caller's other entries.

    **Ordering is not a style choice here.** `ix_education_entries_one_most_recent`
    is a unique partial index on `user_id WHERE is_most_recent AND deleted_at IS
    NULL`, so writing a second most-recent entry before clearing the first
    raises `IntegrityError` — a 500 on an ordinary edit. Clearing first makes the
    constraint a backstop rather than the thing the user meets.
    """
    statement = update(EducationEntry).where(
        EducationEntry.user_id == user_id,
        EducationEntry.is_most_recent.is_(True),
        EducationEntry.deleted_at.is_(None),
    )
    if except_id is not None:
        statement = statement.where(EducationEntry.id != except_id)
    await session.execute(statement.values(is_most_recent=False))


async def create_education(
    session: AsyncSession, user_id: UUID, payload: dict[str, Any]
) -> tuple[UUID, bool]:
    """Add a degree. Returns its id, and whether an institution was created.

    The caller commits. Both writes belong to one transaction, and the session
    is what carries that — committing here would make the institution durable
    before the entry it exists for.
    """
    institution_id, minted = await _resolve_institution(
        session,
        school_name_raw=payload["school_name_raw"],
        supplied=payload.get("institution_id"),
        user_id=user_id,
    )

    if payload.get("is_most_recent"):
        await _clear_most_recent(session, user_id)

    # Only what was sent: an explicit NULL would override a server default.
    values = {key: value for key, value in payload.items() if key in WRITABLE}
    result = await session.execute(
        insert(EducationEntry)
        .values(
            user_id=user_id,
            institution_id=institution_id,
            is_most_recent=bool(payload.get("is_most_recent")),
            **values,
        )
        .returning(EducationEntry.id)
    )
    return result.scalar_one(), minted


async def update_education(
    session: AsyncSession, user_id: UUID, entry_id: UUID, payload: dict[str, Any]
) -> bool:
    """Change a degree. ``False`` when there is no such entry **of theirs**.

    Absent keys are left alone — `PATCH`, not `PUT`. The caller passes only what
    was sent, so a field omitted from the request is not the same as one set to
    null.
    """
    if payload.get("is_most_recent"):
        await _clear_most_recent(session, user_id, except_id=entry_id)

    values = {key: value for key, value in payload.items() if key in WRITABLE}
    if "is_most_recent" in payload:
        values["is_most_recent"] = bool(payload["is_most_recent"])
    if "institution_id" in payload:
        values["institution_id"], _minted = await _resolve_institution(
            session,
            school_name_raw=str(payload.get("school_name_raw") or ""),
            supplied=payload["institution_id"],
            user_id=user_id,
        )
    if not values:
        # Nothing to change, but the entry must still be theirs — otherwise a
        # no-op body would answer 204 for somebody else's row.
        found = await session.execute(
            select(EducationEntry.id).where(
                EducationEntry.id == entry_id,
                EducationEntry.user_id == user_id,
                EducationEntry.deleted_at.is_(None),
            )
        )
        return found.first() is not None

    result = await session.execute(
        update(EducationEntry)
        .where(
            EducationEntry.id == entry_id,
            EducationEntry.user_id == user_id,
            EducationEntry.deleted_at.is_(None),
        )
        .values(**values)
    )
    # `AsyncSession.execute` is typed as `Result`, which has no `rowcount`;
    # a DML statement always returns a `CursorResult`. `asset_store` avoids the
    # cast only because it executes on a connection rather than a session.
    return cast("CursorResult[Any]", result).rowcount > 0


async def delete_education(session: AsyncSession, user_id: UUID, entry_id: UUID) -> bool:
    """Soft-delete. The row survives; the read stops returning it.

    `education_entries` carries `deleted_at`, so this is an `UPDATE`. A real
    `DELETE` here would take the row that `school_name_raw` preserves — the
    thing that makes an unmatched school recoverable rather than lost.
    """
    result = await session.execute(
        update(EducationEntry)
        .where(
            EducationEntry.id == entry_id,
            EducationEntry.user_id == user_id,
            EducationEntry.deleted_at.is_(None),
        )
        .values(deleted_at=func.now())
    )
    return cast("CursorResult[Any]", result).rowcount > 0
