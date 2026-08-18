"""What a mentor offers — to anyone who asks, and to the mentor themselves.

**Two readers with deliberately different answers, in one module because they
read one table.** `list_session_types` is the public one; `list_own_session_types`
is the mentor's own management list, and it drops both the mentor-visibility
predicate and the `is_active` check. Keeping them side by side is the point: the
difference between them *is* the contract, and a reader comparing the two
functions sees it without opening two files.

The public endpoint is also the one that makes `/slots` reachable. Slots require a
`session_type_id` and, until this shipped, nothing handed one out — so the slots
endpoint was correct and unusable from a browse page.

**Two statements, deliberately.** A single query filtered by both the mentor's
visibility and the session types' liveness returns zero rows for two different
situations: a mentor nobody may see, and a visible mentor offering nothing right
now. Those are different answers — 404 and an empty page — and collapsing them
would tell a caller that a mentor who has switched everything off does not
exist. The same shape as `list_session_events`, and for the same reason.

**`meeting_venue` is resolved, in three steps, by `_resolved_venue`** — the
offering's own conferencing option, then the mentor's default, then the platform
fallback. It is no longer a column: `session_type_booking_configs.meeting_venue`
was a label and is now a composite reference to a row the mentor configured.

*What follows is the history of that column, kept because it records why each
move happened rather than only that it did.* It used to `COALESCE` onto
`mentor_profiles.default_meeting_venue`,
because null on a config meant *inherit from the mentor* (package D21). D88 moved
the column here and then removed the inherit entirely: the cascade's terminus was
a state a mentor can legitimately be in — live offerings, no primary — so the
column became `NOT NULL` with a server default and every offering carries its
own. The contract step then dropped the mentor-level column, so there is no
second place a venue could come from.

Settled decision #102 records why the venue left the fallback. It also recorded
that `requires_booking_confirmation` kept it, which #106 has since reversed —
that column went back to `mentor_profiles` and inherits from the *mentor* rather
than from a primary offering. Venue is unaffected and has no mentor-level home.
`models/sessions.py` states both facts at the columns.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, insert, literal, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.errors import ConflictError
from app.domain.enums import ConferencingProvider
from app.infra.db.models.mentoring import MentorConferencingOption, MentorProfile
from app.infra.db.models.sessions import SessionType, SessionTypeBookingConfig
from app.infra.db.models.user import User
from app.infra.db.public_visibility import (
    mentor_is_public,
    session_type_is_live,
    session_type_of,
)

__all__ = [
    "create_session_type",
    "list_own_session_types",
    "list_session_types",
    "update_session_type",
]

#: The offering's own option, joined on the **composite** key.
#:
#: Both columns are in the condition deliberately. `conferencing_option_id` alone
#: would join correctly today and would keep joining correctly if the composite
#: foreign key were ever weakened to a single column — which is exactly the
#: mistake that would let one mentor's offering resolve another mentor's venue,
#: silently and with every test still green.
_chosen = aliased(MentorConferencingOption, name="chosen_option")

#: The mentor's default, for an offering that chose nothing.
_default = aliased(MentorConferencingOption, name="default_option")


def _resolved_venue() -> Any:
    """Where an offering is held: its own option, else the mentor's default, else
    the platform fallback.

    **One copy, because two would drift.** The public list and the owner list both
    need it and a resolution rule written twice is non-negotiable #8.

    **Three steps, and the third is not padding.** Seeding every mentor a default
    row makes step three look unreachable, and *"it cannot happen because creation
    always sets it"* is precisely the reasoning that failed for
    `primary_session_type_id`: it was true until the retirement trigger made
    release-then-retire a legal state, and the venue cascade then had a reachable,
    empty bottom. `SessionTypeRead.meeting_venue` is a **required** field, so a
    resolution that can return null is a 500 waiting for the first mentor who
    slips through. Seed *and* fall back.

    The literal is the enum member's value rather than a bare string, so renaming
    the member moves this with it.
    """
    return func.coalesce(
        _chosen.provider,
        _default.provider,
        literal(ConferencingProvider.GOOGLE_MEET.value),
    ).label("meeting_venue")


def _with_venue(statement: Select[Any]) -> Select[Any]:
    """Attach both option joins. Outer on both — an offering need not have chosen
    one, and a mentor need not have configured any."""
    return statement.outerjoin(
        _chosen,
        (_chosen.id == SessionType.conferencing_option_id)
        & (_chosen.user_id == SessionType.mentor_user_id),
    ).outerjoin(
        _default,
        (_default.user_id == SessionType.mentor_user_id) & _default.is_default,
    )


def _public_mentor(user_id: UUID) -> Select[Any]:
    """Whether this mentor may be seen at all, asked on its own.

    Deliberately selects a constant: nothing about the mentor is needed, only
    whether they exist publicly, and selecting columns nobody reads invites
    somebody to start reading them.
    """
    return (
        select(literal(1))
        .select_from(MentorProfile)
        # `mentor_is_public()` names `users.deleted_at`, so the join is part of
        # the contract rather than an optimisation. See that function for why it
        # is a comparison and not a subquery.
        .join(User, User.id == MentorProfile.user_id)
        .where(MentorProfile.user_id == user_id, *mentor_is_public())
    )


def _live_session_types(user_id: UUID) -> Select[Any]:
    """This mentor's session types, as a stranger sees them.

    **`created_by`, `category` and `application_stage` are absent on purpose.**
    The first is internal attribution, null on every migrated row. The other two
    are free text with no constraint, no vocabulary and no value anywhere in the
    data — publishing them would commit a public contract to a shape nobody has
    designed, and removing a field later is breaking where adding one is not.

    Ordered by name, which is unique per mentor among live rows, so the order is
    total and stable rather than merely usually-stable.
    """
    statement = (
        select(
            SessionType.id,
            SessionType.name,
            SessionType.description,
            SessionTypeBookingConfig.duration_minutes,
            SessionTypeBookingConfig.min_notice_minutes,
            # Resolved, not read. See `_resolved_venue`.
            _resolved_venue(),
        )
        .select_from(SessionType)
        .join(
            SessionTypeBookingConfig,
            SessionTypeBookingConfig.session_type_id == SessionType.id,
        )
        .join(MentorProfile, MentorProfile.user_id == SessionType.mentor_user_id)
        .join(User, User.id == SessionType.mentor_user_id)
    )
    return (
        _with_venue(statement)
        .where(*session_type_is_live(user_id), *mentor_is_public())
        .order_by(SessionType.name)
    )


def _own_session_types(mentor_user_id: UUID) -> Select[Any]:
    """This mentor's session types, as **they** see them.

    **No `mentor_is_public()`, and that absence is the whole point.** The public
    query above answers *what may a stranger book*; this one answers *what have I
    got*. A mentor who is unlisted, still pending review, or paused is exactly the
    mentor most likely to be looking at their own list, and gating this on the
    same predicate would hand them an empty screen at the moment they most need it.

    **No `is_active` either**, which is the other half. `session_type_of()`
    carries ownership and soft deletion only, so a switched-off offering is
    returned and flagged rather than hidden — a management list that silently
    omits what you switched off gives you no way to switch it back on.

    **`category` and `application_stage` are returned here and withheld publicly.**
    That is not a contradiction of the note above: the public reasoning is that
    publishing free text with no vocabulary would commit a *public* contract to an
    undesigned shape, and removing a field later is breaking. Neither applies to
    the mentor who typed the value — they are being shown their own row.

    Ordered by name for the same reason the public query is, and the guarantee
    survives the wider row set: the unique index is
    `(mentor_user_id, name) WHERE deleted_at IS NULL`, whose predicate is soft
    deletion rather than `is_active`, so an inactive row is still covered and the
    order is still total.

    **The join to the config stays inner**, matching the public query. Duration,
    notice and venue all come from that row and there is nothing to read without
    it. **`create_session_type` writes both rows in one transaction**, so this
    still excludes no row the product can reach — the claim used to be "nothing
    can create an offering at all", and it is now the stronger one that nothing
    can create a configless one. A `LEFT JOIN` would make three required response
    fields nullable, which is a contract change and belongs to the release that
    can actually produce such a row.
    """
    statement = (
        select(
            SessionType.id,
            SessionType.name,
            SessionType.description,
            SessionType.category,
            SessionType.application_stage,
            SessionType.is_active,
            SessionTypeBookingConfig.duration_minutes,
            SessionTypeBookingConfig.min_notice_minutes,
            _resolved_venue(),
        )
        .select_from(SessionType)
        .join(
            SessionTypeBookingConfig,
            SessionTypeBookingConfig.session_type_id == SessionType.id,
        )
    )
    return _with_venue(statement).where(*session_type_of(mentor_user_id)).order_by(SessionType.name)


async def list_own_session_types(
    session: AsyncSession, mentor_user_id: UUID
) -> list[dict[str, Any]]:
    """Everything this mentor has, switched on or off.

    **A list rather than ``list | None``, because there is no 404 to express.**
    The public reader returns `None` for a mentor a stranger may not see; here the
    caller *is* the mentor, so the only two answers are their offerings and an
    empty list. A user who is not a mentor at all gets the empty list rather than
    a refusal: `session_types.mentor_user_id` references `mentor_profiles`, so
    they cannot own a row, and "you have none" is a true statement about them.

    **The ownership scope is in the query, never checked after the fetch** —
    non-negotiable #5. There is no row to forget to filter: a caller who is not
    this mentor never sees the row at all, and the reason is the same statement
    that found it.
    """
    result = await session.execute(_own_session_types(mentor_user_id))
    return [dict(row) for row in result.mappings()]


async def list_session_types(session: AsyncSession, user_id: UUID) -> list[dict[str, Any]] | None:
    """Everything this mentor currently offers, or ``None`` if they are not public.

    ``None`` becomes a 404 covering an unapproved mentor, an unlisted one, and a
    user id that is nobody — indistinguishable on purpose, because telling them
    apart says which mentors exist and what state they are in.

    An **empty list** is a different and true statement: this mentor is public
    and is offering nothing bookable at the moment.
    """
    if (await session.execute(_public_mentor(user_id))).first() is None:
        return None

    result = await session.execute(_live_session_types(user_id))
    return [dict(row) for row in result.mappings()]


#: The partial unique index from `20260812_1000_m4_session_type_idempotency_key`,
#: whose name says idempotency and whose job is not that: it is
#: `UNIQUE (mentor_user_id, name) WHERE deleted_at IS NULL`.
#:
#: Named here because the message is matched against it. A bare `IntegrityError`
#: catch would also swallow the booking config's `session_type_id` unique
#: violation and the composite conferencing key, reporting a name clash for
#: neither.
NAME_INDEX = "ix_session_types_mentor_name"


@asynccontextmanager
async def _distinct_names() -> AsyncIterator[None]:
    """Turn the partial unique index into a 409 rather than a 500.

    The index is the mechanism. Selecting first and inserting after is a
    check-then-insert race, and being unraceable is why the invariant lives in
    the schema — so the write is attempted and the refusal translated, which is
    the one order that cannot be raced.

    **Partial on `deleted_at IS NULL`, which is why the message says so.** A
    deleted offering does not reserve its name, and a mentor who deleted
    "SOP review" and is being told the name is taken would have no way to find
    the row holding it.
    """
    try:
        yield
    except IntegrityError as exc:
        if NAME_INDEX in str(exc.orig):
            raise ConflictError("you already have a live session type with this name") from exc
        raise


async def create_session_type(
    session: AsyncSession, mentor_user_id: UUID, payload: dict[str, Any]
) -> UUID | None:
    """A new offering **and its booking config**, or ``None`` for a non-mentor.

    **Both rows or neither, and that is the load-bearing part.** `/slots` and
    both read paths inner-join `session_type_booking_configs`, so an offering
    without one is invisible everywhere and unbookable — a state no endpoint can
    repair, because nothing writes a config on its own. The caller commits once,
    so a failure between the two statements rolls back both; splitting this into
    two endpoints, or committing between them, is what would make the broken
    state reachable.

    **`None` rather than an exception for a caller with no mentor profile.**
    `session_types.mentor_user_id` references `mentor_profiles`, so the insert
    would raise a foreign-key violation — a 500 describing a constraint, for a
    request that is simply not theirs to make. The route turns this into the same
    404 `PATCH /mentor-profile` gives, and it is checked here rather than after
    the fact because the check *is* a query.

    `conferencing_option_id` is left null: it means *use my default*, and nothing
    can create an option yet. The venue still resolves — see `_resolved_venue`.
    """
    owns = await session.execute(
        select(literal(1))
        .select_from(MentorProfile)
        .where(MentorProfile.user_id == mentor_user_id, MentorProfile.deleted_at.is_(None))
    )
    if owns.first() is None:
        return None

    async with _distinct_names():
        session_type_id = (
            await session.execute(
                insert(SessionType)
                .values(
                    mentor_user_id=mentor_user_id,
                    name=payload["name"],
                    description=payload.get("description"),
                    category=payload.get("category"),
                    application_stage=payload.get("application_stage"),
                )
                .returning(SessionType.id)
            )
        ).scalar_one()

        # Inside the same block and the same transaction. `/slots` and both read
        # paths inner-join this row, so an offering without one is invisible and
        # unbookable with nothing able to repair it.
        await session.execute(
            insert(SessionTypeBookingConfig).values(
                session_type_id=session_type_id,
                duration_minutes=payload["duration_minutes"],
                min_notice_minutes=payload["min_notice_minutes"],
            )
        )
    return session_type_id


#: Which payload keys belong to which table. Split here rather than at the
#: boundary because the write model is one shape by design — a mentor edits *an
#: offering*, and that it spans two tables is this layer's problem.
SESSION_TYPE_COLUMNS = ("name", "description", "category", "application_stage", "is_active")
BOOKING_CONFIG_COLUMNS = ("duration_minutes", "min_notice_minutes")


async def update_session_type(
    session: AsyncSession, mentor_user_id: UUID, session_type_id: UUID, payload: dict[str, Any]
) -> bool:
    """Change one offering. ``False`` if it is not this mentor's, or is deleted.

    **Scoped with `session_type_of()`, which is the narrower predicate and not a
    flag on the live one.** The owner path needs ownership and soft deletion but
    **not** `is_active` — a mentor editing a switched-off offering is the ordinary
    case, and it is what switching it back on requires. Adding `include_inactive`
    to `session_type_is_live()` instead would touch the predicate that decides
    what is *bookable*, which `slot_store` spreads: a mis-defaulted flag reaching
    it makes deactivated offerings bookable again, against settled decision #90,
    silently and one keyword away. A predicate taking no argument cannot carry
    that mistake.

    **Not-yours and not-found are the same answer** (house convention), and here
    they are the same *statement*: the scope is in the `WHERE`, so a row
    belonging to somebody else is not found rather than found and refused.

    The config `UPDATE` runs only when the payload touches it, so a rename does
    not rewrite `updated_at` on a row nothing changed.
    """
    scoped = select(SessionType.id).where(
        *session_type_of(mentor_user_id), SessionType.id == session_type_id
    )
    if (await session.execute(scoped)).first() is None:
        return False

    own = {key: value for key, value in payload.items() if key in SESSION_TYPE_COLUMNS}
    if own:
        async with _distinct_names():
            await session.execute(
                update(SessionType).where(SessionType.id == session_type_id).values(**own)
            )

    config = {key: value for key, value in payload.items() if key in BOOKING_CONFIG_COLUMNS}
    if config:
        await session.execute(
            update(SessionTypeBookingConfig)
            .where(SessionTypeBookingConfig.session_type_id == session_type_id)
            .values(**config)
        )
    return True
