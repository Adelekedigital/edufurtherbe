"""The public mentor profile, Explore, featured and similar mentors."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import Depends, Query
from pydantic import StringConstraints

from app.api.deps.core import OptionalViewerDep, SessionDep
from app.api.deps.session_types import _with_forms
from app.api.schemas.common import (
    MAX_PAGE_SIZE,
    StorableText,
    clamp_limit,
    decode_browse_cursor,
    decode_goal_cursor,
    decode_offset_cursor,
    encode_browse_cursor,
    is_goal_cursor,
    next_goal_cursor,
    next_offset_cursor,
)
from app.core.errors import (
    NotFoundError,
    ValidationError,
)
from app.infra.db.featured_store import (
    current_featured,
)
from app.infra.db.mentor_public_store import (
    get_existing_mentor_id,
    get_public_mentor,
)
from app.infra.db.mentor_search_store import (
    count_mentors,
    mentor_card,
    search_mentors,
    similar_mentors,
)
from app.infra.db.offerings import has_live_goals, live_offering_slugs, offerings_for
from app.infra.db.profile_store import (
    list_awards,
    list_education,
    list_languages,
)
from app.infra.db.review_stats import mentor_review_stats
from app.infra.db.session_stats import mentor_stats

# `get_session` is aliased: this module already has one, and it is the **database
# session** dependency at line 142. Two callables with that name in one file is a
# collision a reader resolves by scrolling, and the wrong one is a plausible
# mistake rather than an obvious error — `bubble_id` shadowed a local the same
# way in the M4 transform and raised `UnboundLocalError` far from the edit.
from app.infra.db.session_type_store import (
    list_session_types,
)

# --------------------------------------------------------------------------
# The public mentor profile
# --------------------------------------------------------------------------


async def public_mentor(
    handle: str, session: SessionDep, viewer: OptionalViewerDep
) -> dict[str, Any]:
    """One mentor, as a stranger sees them, or a 404 that says nothing about why.

    **Composed rather than re-queried.** The session types come from
    `list_session_types()` — the same function serving `/session-types` — so the
    inlined list and the standalone endpoint cannot drift. It re-checks the
    mentor's visibility, which is a second cheap statement rather than a
    "skip the check" variant, because a guard with a bypass parameter is a guard
    with a bypass.

    `handle` is an id or a slug and the store resolves either. It is not a `UUID`
    in the signature for that reason: FastAPI would refuse a slug at the door with
    a 422, which would tell a caller that the id they guessed was well-formed.

    **`viewer` widens it to the owner and nobody else** — a mentor reads their
    own profile in any state (`mentor_is_visible_to`). The lists below are not
    widened: `session_types` is what a stranger can book, so a hidden profile
    shows none, and the mentor's own list is `/me/session-types`.
    """
    row = await get_public_mentor(session, handle, viewer)
    if row is None:
        raise NotFoundError("no such mentor")

    user_id = row["user_id"]
    # Six statements for one profile, and that is a decision rather than an
    # accident. Each list is a different table with a different scope, so they
    # cannot be one join without a fan-out to unpick in Python; and a profile is
    # a single-resource read a client makes once per page, not per row of a list.
    # The alternative — six round trips from the browser — is worse for the same
    # work. `mentor_page` is where a count like this would be a defect.
    return {
        "row": row,
        "offerings": (await offerings_for(session, [user_id])).get(user_id, []),
        "session_types": await _with_forms(session, await list_session_types(session, user_id)),
        "education": await list_education(session, user_id),
        "scholarships": await list_awards(session, user_id),
        "languages": await list_languages(session, user_id),
        "stats": await mentor_stats(session, user_id),
        "reviews": await mentor_review_stats(session, user_id),
    }


#: An input bound, not the taxonomy's size: #53 closes the list at six, and a
#: second copy of that number here would be the one that drifts.
MAX_OFFERING_FILTERS = 10

#: An input bound on one offering slug. The column is unbounded `text`; the six
#: platform-authored slugs are all far shorter, and this only keeps a 422's echo
#: of what the caller sent short.
MAX_SLUG_LENGTH = 60


async def mentor_page(
    session: SessionDep,
    viewer: OptionalViewerDep,
    q: Annotated[
        str | None,
        Query(description="Search mentors by name, school, programme or country."),
        StorableText,
    ] = None,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int | None, Query(ge=1, le=MAX_PAGE_SIZE)] = None,
    offering: Annotated[
        # `StorableText` per item, not on the list: a NUL in a slug reached
        # Postgres and was a 500 from one anonymous request — #97's fourth
        # parameter. The length bound keeps what a 422 echoes back short.
        list[Annotated[str, StringConstraints(max_length=MAX_SLUG_LENGTH), StorableText]] | None,
        Query(
            max_length=MAX_OFFERING_FILTERS,
            description=(
                "A service-offering slug from `/api/v1/catalog/service-offerings`. "
                "Repeatable, and **any** one matches: "
                "`?offering=a&offering=b` lists mentors who give either. A "
                "`q` search is narrowed to those mentors. An unknown or retired slug is a `422`."
            ),
        ),
    ] = None,
) -> tuple[list[dict[str, Any]], bool, str | None, int | None]:
    """One page of bookable mentors, browsing or searching.

    **The mode decides how the token is read**, which is why decoding happens
    here rather than in the store: a browse cursor and a search cursor are both
    opaque base64 and are not interchangeable, so each decoder refuses the
    other's tag and a mixed request is a 422 rather than a confidently wrong
    page.

    The third element of the return is the mode, so the route knows which kind of
    token to mint without re-deriving it from `q` and risking the two disagreeing.
    """
    # Normalised **here and only here**. The store used to strip and test `q`
    # again, so a broken decision in this function was masked by the store's
    # copy quietly doing the right thing — one rule in two places, invisible
    # precisely because the two agreed. Now this decides and the store trusts.
    term = (q or "").strip() or None

    # Refused rather than matching nobody: a typo'd slug would otherwise render
    # as "no mentors" and read as a supply problem. Retired counts as unknown,
    # for the reason `offerings._live()` gives.
    # A value that empties is no filter, as a blank `q` is no search.
    slugs = list(dict.fromkeys(s for s in (v.strip() for v in offering or ()) if s))
    unknown = set(slugs) - await live_offering_slugs(session, slugs)
    if unknown:
        raise ValidationError(f"unknown offering: {', '.join(sorted(unknown))}")

    # Counted on the first page only: on the search path the count is a second
    # sequential scan, and a client paging on already has the number.
    total = (
        await count_mentors(session, q=term, offerings=slugs, viewer=viewer)
        if cursor is None
        else None
    )

    # **A signed-in mentee with goals gets the goal ranking** (settled decision
    # #187), keyed on today's UTC date so the tie shuffle holds all day. A
    # search outranks it — that precedence is `search_mentors`'s, which reads
    # `q` first; `term is None` here only skips a goals lookup a search would
    # ignore, so dropping it changes no response (an equivalent mutant).
    goal_day = (
        dt.datetime.now(dt.UTC).date()
        if term is None and viewer is not None and await has_live_goals(session, viewer)
        else None
    )

    # **Once paging has begun, the cursor's kind decides the mode**, not who the
    # viewer is now. A token can lapse between pages, a visitor can sign in, a
    # mentee can add a first goal: re-deciding from the viewer would hand one
    # kind of cursor to the other's decoder and answer a list anyone may read
    # with a 422. A goal cursor continues by offset — goal-ranked if the viewer
    # still has goals, else newest first from that position; an id cursor
    # continues newest first. A *search* cursor without its `q` is still
    # refused: that is a client that dropped its query, not a changed viewer.
    goal_paging = term is None and (
        is_goal_cursor(cursor) if cursor is not None else goal_day is not None
    )
    if goal_paging:
        offset = decode_goal_cursor(cursor) if cursor is not None else 0
        rows, has_more = await search_mentors(
            session,
            limit=clamp_limit(limit),
            offset=offset,
            offerings=slugs,
            viewer=viewer,
            goal_day=goal_day,
        )
        next_cursor = next_goal_cursor(offset + len(rows)) if has_more else None
        return rows, has_more, next_cursor, total

    # Search pages by offset too — its order is not a column in the row — and
    # shares the depth cap.
    if term is not None:
        offset = decode_offset_cursor(cursor)
        rows, has_more = await search_mentors(
            session,
            limit=clamp_limit(limit),
            q=term,
            offset=offset,
            offerings=slugs,
            viewer=viewer,
            goal_day=goal_day,
        )
        # `next_offset_cursor`, not `encode_offset_cursor`: past the depth cap
        # there is no next page, and minting one the decoder then refuses ends a
        # deep search on a 422 for a client that followed the envelope exactly.
        next_cursor = next_offset_cursor(offset + len(rows)) if has_more else None
        return rows, has_more, next_cursor, total

    rows, has_more = await search_mentors(
        session,
        limit=clamp_limit(limit),
        after=decode_browse_cursor(cursor),
        offerings=slugs,
        viewer=viewer,
    )  # `goal_day` is not passed: an id cursor continues newest first.
    next_cursor = (
        encode_browse_cursor(rows[-1]["taking_bookings"], rows[-1]["cursor_id"])
        if has_more and rows
        else None
    )
    return rows, has_more, next_cursor, total


MentorPageDep = Annotated[
    tuple[list[dict[str, Any]], bool, str | None, int | None], Depends(mentor_page)
]

PublicMentorDep = Annotated[dict[str, Any], Depends(public_mentor)]


async def featured_mentor(session: SessionDep) -> dict[str, Any] | None:
    """This week's featured mentor as a card, or `None` when nobody is.

    `current_featured` only returns someone still taking bookings — the pick and
    the re-read both filter on `bookable_mentors()` — so a mentor who stopped
    being bookable is replaced rather than shown. `None` too if they stopped
    being visible between the pick and the read.
    """
    mentor = await current_featured(session, now=dt.datetime.now(dt.UTC))
    return None if mentor is None else await mentor_card(session, mentor)


FeaturedMentorDep = Annotated[dict[str, Any] | None, Depends(featured_mentor)]


async def similar_to_mentor(handle: str, session: SessionDep) -> list[dict[str, Any]]:
    """Up to three bookable mentors like this one — whatever state this one is in.

    **Resolved for any existing mentor, live or not** (owner, 2026-09-29,
    superseding #186's 404). A visitor who opens a pending, unlisted or
    unbookable mentor's link lands on a page that cannot show the profile, and
    offers mentors with the same expertise instead. The suggestions themselves
    stay live-only, which `similar_mentors` already guarantees.

    **Nobody is an empty list, not a 404.** Answering "no such mentor" for an
    unknown handle while answering a hidden one would tell anyone which hidden
    mentors exist; an empty list is also the "see other mentors" state a
    mistyped link should get. The same answer for every viewer, so it stays
    shareable-cacheable.
    """
    mentor = await get_existing_mentor_id(session, handle)
    if mentor is None:
        return []
    return await similar_mentors(session, mentor)


SimilarMentorsDep = Annotated[list[dict[str, Any]], Depends(similar_to_mentor)]
