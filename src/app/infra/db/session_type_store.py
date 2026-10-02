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

import datetime as dt
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, insert, literal, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.errors import ConflictError, ValidationError
from app.domain.availability import BookingWindow
from app.domain.enums import ApplicationStage, ConferencingProvider
from app.domain.meetings import PLATFORM_DEFAULT_PROVIDER
from app.domain.sessions import first_stage, named_stages, stage_label_problem
from app.infra.db.booking_rules import (
    effective_duration_minutes,
    effective_min_notice_minutes,
    effective_window_days,
    inherits_duration,
    inherits_min_notice,
)
from app.infra.db.intake_store import live_question_count
from app.infra.db.models.mentoring import (
    MentorConferencingOption,
    MentorProfile,
)
from app.infra.db.models.sessions import (
    LIVE_STATUSES,
    Session,
    SessionType,
    SessionTypeBookingConfig,
)
from app.infra.db.models.user import User
from app.infra.db.offerings import (
    offerings_for_session_types,
    set_session_type_offerings,
)
from app.infra.db.public_visibility import (
    has_own_windows,
    mentor_is_public,
    session_type_is_live,
    session_type_of,
)
from app.infra.db.stages import stages_for_session_types, write_session_type_stages

__all__ = [
    "DeletionScheduled",
    "create_session_type",
    "delete_session_type",
    "finalise_scheduled_deletions",
    "get_own_session_type",
    "list_own_session_types",
    "list_session_types",
    "restore_session_type",
    "update_session_type",
]


def _live_on(session_type_id: Any) -> list[Any]:
    """The sessions still holding an offering open: awaiting a decision, or agreed.

    `LIVE_STATUSES` reused, never retyped — it is the predicate behind the
    double-booking constraint and three partial indexes, and the one that decides
    whether a delete goes now or waits (#218).
    """
    return [Session.session_type_id == session_type_id, text(LIVE_STATUSES)]


def _booked_count(session_type_id: Any) -> Any:
    """How many live sessions hold this offering, as a scalar subquery."""
    return (
        select(func.count())
        .select_from(Session)
        .where(*_live_on(session_type_id))
        .scalar_subquery()
    )


def _deletes_after(session_type_id: Any) -> Any:
    """When the last live session on this offering ends, or null if none is left.

    The end is `session_window(...)`'s upper bound — the function the
    double-booking constraint uses, so "when a session ends" has one definition.
    """
    return (
        select(
            func.max(func.upper(func.session_window(Session.starts_at, Session.duration_minutes)))
        )
        .where(*_live_on(session_type_id))
        .scalar_subquery()
    )


#: Featured first, then by name — unique per mentor among live rows, so the
#: order is total (#217).
_LISTED = (SessionType.is_featured.desc(), SessionType.name)


@dataclass(frozen=True, slots=True)
class DeletionScheduled:
    """A delete that waits for the offering's booked sessions (#218)."""

    deletes_after: dt.datetime
    booked_count: int


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

    The literal is `PLATFORM_DEFAULT_PROVIDER` (EduFurther video since
    2026-10-01), the same constant `/me/conferencing` reports for a mentor who
    never chose.
    """
    return func.coalesce(
        _chosen.provider,
        _default.provider,
        literal(PLATFORM_DEFAULT_PROVIDER.value),
    ).label("meeting_venue")


async def resolve_venue(
    session: AsyncSession, session_type_id: UUID
) -> tuple[ConferencingProvider, str | None] | None:
    """Where this offering is held, and its URL if the mentor supplied one.

    **Here rather than in the writer, so the precedence has one home.** The two
    read models already resolve a venue through `_resolved_venue`, and
    provisioning must agree with what a mentee was shown — an offering listed as
    held on Daily that mints a Meet link is a contract broken silently.

    The custom URL rides along because it is the one venue nothing creates: for
    `custom` the URL *is* the resolution, and fetching it in a second query
    would let the two disagree about which option row won.

    ``None`` when the offering does not exist. Unreachable from the writer,
    which has already resolved the offering to book it, and returned rather than
    raised so a caller cannot mistake an absence for a default.
    """
    row = (
        (
            await session.execute(
                _with_venue(
                    select(
                        _resolved_venue(),
                        func.coalesce(_chosen.custom_url, _default.custom_url).label("custom_url"),
                    ).select_from(SessionType)
                ).where(SessionType.id == session_type_id)
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    return ConferencingProvider(str(row["meeting_venue"])), row["custom_url"]


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


def _live_session_types(user_id: UUID, window: BookingWindow) -> Select[Any]:
    """This mentor's session types, as a stranger sees them.

    **`created_by` is absent on purpose** — internal attribution, null on every
    migrated row.

    **`service_offering_id` and `application_stage` are no longer absent.** They
    were withheld while they were free text with no vocabulary, because
    publishing them would have committed a public contract to a shape nobody had
    designed. Both have a designed shape now — a reference to the closed taxonomy
    and a five-value closed set — so the reason lapsed rather than being
    overruled. Removing a field later is breaking where adding one is not, which
    is why the bar for adding was high and is now met.

    Ordered by name, which is unique per mentor among live rows, so the order is
    total and stable rather than merely usually-stable.
    """
    statement = (
        select(
            SessionType.id,
            SessionType.name,
            SessionType.description,
            SessionType.application_stage,
            SessionType.custom_stage_label,
            SessionType.icon,
            SessionType.is_featured,
            # Resolved, not read (#216): the offering's own, else its mentor's
            # default, else the platform's. The field stays an int.
            effective_duration_minutes().label("duration_minutes"),
            effective_min_notice_minutes().label("min_notice_minutes"),
            # Resolved and clamped (Round 5): what the booking modal may show.
            effective_window_days(window).label("booking_window_days"),
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
        .order_by(*_LISTED)
    )


def _own_session_types(mentor_user_id: UUID, window: BookingWindow) -> Select[Any]:
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

    **`category` and `application_stage` used to be returned here and withheld
    publicly, and that asymmetry is gone.** The public reasoning was that
    publishing free text with no vocabulary would commit a *public* contract to
    an undesigned shape; both columns have a shape now, so both lists carry them.
    What still differs is `is_active` — the public list cannot express a paused
    offering because it does not return one.

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
            SessionType.application_stage,
            SessionType.custom_stage_label,
            SessionType.icon,
            SessionType.is_active,
            SessionType.is_featured,
            SessionType.deletion_scheduled_at,
            # Derived, so a cancellation moves them without a write (#218).
            _booked_count(SessionType.id).label("booked_count"),
            _deletes_after(SessionType.id).label("deletes_after"),
            effective_duration_minutes().label("duration_minutes"),
            effective_min_notice_minutes().label("min_notice_minutes"),
            inherits_duration().label("duration_inherited"),
            inherits_min_notice().label("min_notice_inherited"),
            SessionTypeBookingConfig.requires_booking_confirmation,
            SessionTypeBookingConfig.booking_window_days,
            effective_window_days(window).label("effective_booking_window_days"),
            SessionTypeBookingConfig.break_after_minutes,
            # Its own windows, or its mentor's Calendar hours (#199, frontend #146).
            has_own_windows(SessionType.id).label("uses_own_windows"),
            # The form's size, so the list needs no per-type call (frontend #147).
            live_question_count(SessionType.id).label("question_count"),
            _resolved_venue(),
        )
        .select_from(SessionType)
        .join(
            SessionTypeBookingConfig,
            SessionTypeBookingConfig.session_type_id == SessionType.id,
        )
        # For the inherited length and notice (#216). Inner, because
        # `session_types.mentor_user_id` references this table.
        .join(MentorProfile, MentorProfile.user_id == SessionType.mentor_user_id)
    )
    return _with_venue(statement).where(*session_type_of(mentor_user_id)).order_by(*_LISTED)


async def list_own_session_types(
    session: AsyncSession, mentor_user_id: UUID, *, window: BookingWindow
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
    result = await session.execute(_own_session_types(mentor_user_id, window))
    return await _with_sets(session, [dict(row) for row in result.mappings()])


async def get_own_session_type(
    session: AsyncSession, mentor_user_id: UUID, session_type_id: UUID, *, window: BookingWindow
) -> dict[str, Any] | None:
    """One of this mentor's offerings, as their list shows it, or ``None``."""
    result = await session.execute(
        _own_session_types(mentor_user_id, window).where(SessionType.id == session_type_id)
    )
    rows = await _with_sets(session, [dict(row) for row in result.mappings()])
    return rows[0] if rows else None


async def list_session_types(
    session: AsyncSession, user_id: UUID, *, window: BookingWindow
) -> list[dict[str, Any]] | None:
    """Everything this mentor currently offers, or ``None`` if they are not public.

    ``None`` becomes a 404 covering an unapproved mentor, an unlisted one, and a
    user id that is nobody — indistinguishable on purpose, because telling them
    apart says which mentors exist and what state they are in.

    An **empty list** is a different and true statement: this mentor is public
    and is offering nothing bookable at the moment.
    """
    if (await session.execute(_public_mentor(user_id))).first() is None:
        return None

    result = await session.execute(_live_session_types(user_id, window))
    return await _with_sets(session, [dict(row) for row in result.mappings()])


async def _with_sets(session: AsyncSession, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach each type's offerings (#205) and stages (#215) — one extra
    statement each for the whole list."""
    ids = [row["id"] for row in rows]
    offerings = await offerings_for_session_types(session, ids)
    stages = await stages_for_session_types(session, ids)
    for row in rows:
        row["service_offerings"] = offerings.get(row["id"], [])
        row["application_stages"] = stages.get(row["id"], [])
    return rows


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

    stages = named_stages(payload) or []
    _refuse_stage_label(stages, payload.get("custom_stage_label"))
    async with _distinct_names():
        session_type_id = (
            await session.execute(
                insert(SessionType)
                .values(
                    mentor_user_id=mentor_user_id,
                    name=payload["name"],
                    description=payload.get("description"),
                    application_stage=first_stage(stages),
                    custom_stage_label=payload.get("custom_stage_label"),
                    icon=payload.get("icon"),
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
                # Null inherits (#216), written explicitly: an absent key would
                # take `min_notice_minutes`' server default and stop inheriting.
                duration_minutes=payload.get("duration_minutes"),
                min_notice_minutes=payload.get("min_notice_minutes"),
                requires_booking_confirmation=payload.get("requires_booking_confirmation"),
                booking_window_days=payload.get("booking_window_days"),
                break_after_minutes=payload.get("break_after_minutes"),
            )
        )
    if stages:
        await write_session_type_stages(session, session_type_id, stages)
    offerings = _requested_offerings(payload)
    if offerings is not None:
        await set_session_type_offerings(session, session_type_id, offerings)
    return session_type_id


def _refuse_stage_label(stages: list[ApplicationStage], label: str | None) -> None:
    """The stage set and label the row will end up with, judged whole (#215).

    Asked **before** writing, against the final state, because the `CHECK` sees
    only the first stage and a `PATCH` may change either half alone.
    """
    problem = stage_label_problem(stages, label)
    if problem is not None:
        raise ValidationError(problem[1], field_errors=(problem,))


def _requested_offerings(payload: dict[str, Any]) -> list[UUID] | None:
    """The set a write asks for, or `None` when it names none (#205).

    `service_offering_ids` is the set; the legacy `service_offering_id` is a set
    of one, and `null` there clears it. The boundary refuses a payload sending
    both, so at most one is present here.
    """
    if "service_offering_ids" in payload and payload["service_offering_ids"] is not None:
        return list(payload["service_offering_ids"])
    if "service_offering_id" in payload:
        value = payload["service_offering_id"]
        return [value] if value is not None else []
    return None


#: Which payload keys belong to which table. Split here rather than at the
#: boundary because the write model is one shape by design — a mentor edits *an
#: offering*, and that it spans two tables is this layer's problem.
#:
#: `application_stage` is absent on purpose: it is derived from the stage set
#: and written beside the label in one statement (#215).
SESSION_TYPE_COLUMNS = (
    "name",
    "description",
    "custom_stage_label",
    "icon",
    "is_active",
    "is_featured",
)
BOOKING_CONFIG_COLUMNS = (
    "duration_minutes",
    "min_notice_minutes",
    "requires_booking_confirmation",
    "booking_window_days",
    "break_after_minutes",
)


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
    # **Featuring locks every offering of this mentor first, in id order** (#217),
    # so two requests featuring two different offerings serialise: each clears
    # "the others" and sets its own, and interleaved they would meet on
    # `ix_session_types_one_featured` as a 500. The order is what keeps two of
    # them from deadlocking on each other's rows. **Scheduled rows are left
    # out**: they cannot be featured, and locking them would make a feature wait
    # on — or deadlock with — the settle run finalising them (#218).
    if payload.get("is_featured") is True:
        await session.execute(
            select(SessionType.id)
            .where(
                *session_type_of(mentor_user_id),
                SessionType.deletion_scheduled_at.is_(None),
            )
            .order_by(SessionType.id)
            .with_for_update()
        )

    # **Locked**, so two edits of one offering serialise: each replaces the
    # stage and offering sets by delete-then-insert, and two interleaved would
    # meet on the sets' unique indexes as a 500 rather than last-write-wins.
    scoped = (
        select(SessionType.is_active, SessionType.deletion_scheduled_at)
        .where(*session_type_of(mentor_user_id), SessionType.id == session_type_id)
        .with_for_update()
    )
    state = (await session.execute(scoped)).first()
    if state is None:
        return False

    own = {key: value for key, value in payload.items() if key in SESSION_TYPE_COLUMNS}
    _refuse_visibility(own, shown=state.is_active, scheduled=state.deletion_scheduled_at)
    if own.get("is_featured") is True:
        await session.execute(
            update(SessionType)
            .where(
                *session_type_of(mentor_user_id),
                SessionType.id != session_type_id,
                SessionType.is_featured.is_(True),
            )
            .values(is_featured=False)
        )
    stages = named_stages(payload)
    if stages is not None or "custom_stage_label" in payload:
        current = (
            await session.execute(
                select(SessionType.custom_stage_label).where(SessionType.id == session_type_id)
            )
        ).scalar_one()
        final = (
            stages
            if stages is not None
            else (await stages_for_session_types(session, [session_type_id])).get(
                session_type_id, []
            )
        )
        _refuse_stage_label(final, payload.get("custom_stage_label", current))
    if stages is not None:
        own["application_stage"] = first_stage(stages)
        await write_session_type_stages(session, session_type_id, stages)
    if own:
        async with _distinct_names():
            await session.execute(
                update(SessionType).where(SessionType.id == session_type_id).values(**own)
            )

    offerings = _requested_offerings(payload)
    if offerings is not None:
        await set_session_type_offerings(session, session_type_id, offerings)

    config = {key: value for key, value in payload.items() if key in BOOKING_CONFIG_COLUMNS}
    if config:
        await session.execute(
            update(SessionTypeBookingConfig)
            .where(SessionTypeBookingConfig.session_type_id == session_type_id)
            .values(**config)
        )
    return True


def _refuse_visibility(own: dict[str, Any], *, shown: bool, scheduled: dt.datetime | None) -> None:
    """Featured only if shown; shown only if not scheduled to go (#217, #218).

    Judged on the offering's state **after** the write, so `{is_active: true,
    is_featured: true}` on a hidden one is allowed. Hiding un-features in the
    same write, and stays un-featured if shown again — featuring is a choice
    made again, not a state resumed. Mutates ``own``.
    """
    if own.get("is_active") is True and scheduled is not None:
        raise ValidationError(
            "this offering is scheduled for deletion; restore it before showing it",
            field_errors=(("/is_active", "restore it before showing it"),),
        )
    will_show = own.get("is_active", shown)
    if own.get("is_featured") is True and (not will_show or scheduled is not None):
        raise ValidationError(
            "only an offering that is on offer can be featured",
            field_errors=(("/is_featured", "show this offering before featuring it"),),
        )
    if own.get("is_active") is False:
        own["is_featured"] = False


async def delete_session_type(
    session: AsyncSession, mentor_user_id: UUID, session_type_id: UUID
) -> bool | DeletionScheduled:
    """Delete one offering now, or schedule it behind its booked sessions (#218).

    ``False`` if it is not this mentor's, or is gone; ``True`` if it was deleted
    now; a `DeletionScheduled` if live sessions hold it. **Scheduling replaces
    the `409` this used to be** (#197): the design no longer refuses, it waits.
    The offering is hidden and un-featured at once — hidden is what makes it
    unbookable, so nothing new can land on it — and the hourly settle run
    deletes it once nothing live remains (`finalise_scheduled_deletions`).
    **Idempotent**: a second call on a scheduled offering answers the same
    schedule with current figures, and keeps the first request's time.

    **`LIVE_STATUSES` is reused, not retyped.** It is the predicate behind the
    double-booking exclusion constraint and three partial indexes, and a
    predicate inside a `text()` string is not a symbol any linter can bind — the
    exact shape that put `deleted_at IS NULL` into five statements here with the
    fifth missed. A cancelled or completed session does **not** block deletion;
    only a session still awaiting a decision or already agreed does.

    **Checked in application code, and that is forced rather than preferred.**
    A soft delete is an `UPDATE`, so `sessions.session_type_id`'s `RESTRICT`
    never fires — the same blindness that made `trg_refuse_retiring_a_primary_offering`
    necessary for the pointer. A trigger would close the race and this does not:
    a booking landing between the check and the update leaves a live session on a
    deleted offering. That is survivable and the alternative is not free — PR 13
    removed this schema's only business-rule trigger, and reintroducing one for a
    race whose loser still has a readable session is a trade worth naming rather
    than making silently. `GET /sessions/{id}` does not consult the offering, so
    the mentee keeps their session either way.

    **Soft, never hard.** `sessions.session_type_id` is `RESTRICT`, so an offering
    that was ever booked can never be hard-deleted, and the row is what a past
    session's `session_type_id` still points at.
    """
    scoped = (
        select(SessionType.id)
        .where(*session_type_of(mentor_user_id), SessionType.id == session_type_id)
        .with_for_update()
    )
    if (await session.execute(scoped)).first() is None:
        return False

    held = (
        await session.execute(
            select(_booked_count(session_type_id), _deletes_after(session_type_id))
        )
    ).one()
    if not held[0]:
        await session.execute(
            update(SessionType)
            .where(SessionType.id == session_type_id)
            .values(deleted_at=func.now(), is_featured=False)
        )
        return True

    await session.execute(
        update(SessionType)
        .where(SessionType.id == session_type_id)
        .values(
            is_active=False,
            is_featured=False,
            deletion_scheduled_at=func.coalesce(SessionType.deletion_scheduled_at, func.now()),
        )
    )
    return DeletionScheduled(deletes_after=held[1], booked_count=int(held[0]))


async def restore_session_type(
    session: AsyncSession, mentor_user_id: UUID, session_type_id: UUID
) -> bool:
    """Cancel a scheduled deletion; the offering stays hidden (#218).

    ``False`` if it is not this mentor's, or is gone. A no-op on an offering
    with nothing scheduled — the answer is the same either way, which is what a
    retried tap needs.
    """
    restored = await session.execute(
        update(SessionType)
        .where(*session_type_of(mentor_user_id), SessionType.id == session_type_id)
        .values(deletion_scheduled_at=None)
        .returning(SessionType.id)
    )
    return restored.first() is not None


async def finalise_scheduled_deletions(session: AsyncSession) -> int:
    """Delete every scheduled offering no live session holds any more; how many.

    Run by the hourly `settle-sessions` job, after attendance is settled, so a
    session that ended this hour no longer counts. Idempotent: a deleted row no
    longer matches. Does not commit.

    The check here is `NOT EXISTS` at run time rather than the count taken
    when it was scheduled, so a session that committed before this statement
    still holds the offering open.

    **Not race-free, and accepted with #197's race** (#218). Scheduling hides
    the offering and booking reads `session_type_is_live()`, so no *new* booking
    is offered one — but a booking already past that read when the offering was
    hidden can still commit, and nothing here waits for it: the booking's
    foreign key takes `FOR KEY SHARE` on the offering row, which does not
    conflict with this non-key `UPDATE`. A session can then land on an offering
    this run deletes; it stays readable through `GET /sessions/{id}`. Closed by
    the same revisit trigger as the delete's own window: `FOR SHARE` on the
    offering in `book_session`.
    """
    result = await session.execute(
        update(SessionType)
        .where(
            SessionType.deletion_scheduled_at.is_not(None),
            SessionType.deleted_at.is_(None),
            ~select(Session.id).where(*_live_on(SessionType.id)).exists(),
        )
        .values(deleted_at=func.now())
        .returning(SessionType.id)
    )
    return len(result.all())
