"""Finding a mentor — the only endpoint that answers "who is there at all".

Three public reads existed before this and every one of them needed an id or a
slug you already had. This is the one that hands them out.

**Bookable, never available.** The scope is `mentor_is_public()` plus
`mentor_is_bookable()`: approved, listed, not deleted either way, and set up —
a live session type with a booking config, and a live availability rule. It
says nothing about *when*, because availability is a computation over projected
windows minus bookings and cannot be a `WHERE` clause. Filtering on it would
mean computing slots for every candidate before paging, which stops the cursor
being a database keyset; and caching it in a column is the drift D20 rejected,
where a stored `is_available` was wrong the moment somebody booked. *When* is
what `/slots` answers, freshly, one click later.

**No filters.** Service, school, degree, country of study and country of origin
are all reachable from existing tables, and four of the indexes they would want
already exist. They are not here because a query parameter is additive and a
sort order is not (rule #21) — what has to be right today is the shape a client
renders and the cursor it pages with. Three of those five filters read through
`education_entries`, which is one-to-many, so they arrive as `EXISTS` clauses
rather than joins when they arrive at all.

**Ordered by `mentor_profiles.id`, not `users.id`.** Both are UUIDv7 and both are
therefore time-ordered, but they order different events: when somebody signed up
against when they became a mentor. A mentee of two years who started mentoring
last week is a new mentor, and this list is of mentors.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, literal, literal_column, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.db.models.education import DegreeLevel, EducationEntry, Institution
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.reference import Country
from app.infra.db.models.sessions import Session
from app.infra.db.models.user import User, UserProfile
from app.infra.db.offerings import offerings_for
from app.infra.db.public_visibility import mentor_is_bookable, mentor_is_public
from app.infra.db.qualifications import top_qualification
from app.infra.db.review_stats import card_summary
from app.infra.db.session_stats import delivered

__all__ = ["search_mentors"]

#: `english` stems, which is right for prose and wrong for names. Named rather
#: than inlined so the two never drift apart across the document and the query.
SIMPLE = "simple"
ENGLISH = "english"

_STUDY_COUNTRY = Country.__table__.alias("study_country")

#: Aliased so the lateral can name its columns without colliding with any
#: future join to the same catalogue in the outer query.
_DEGREE_LEVEL = DegreeLevel.__table__.alias("degree_level")


#: The mentor's origin country, aliased separately from where they studied.
_ORIGIN_COUNTRY = Country.__table__.alias("origin_country")


def _document() -> Any:
    """Everything about a mentor that a search term may match, as one `tsvector`.

    **Two configurations, chosen per field rather than for the document.**
    `english` stems, which is right for prose — "studying" finds "study" — and
    wrong for proper nouns, where it turns *Harding* into `hard` and returns that
    mentor to anybody searching for hard work. Four of these fields are names, so
    they take `simple`; the three prose fields take `english`. A `tsvector` is
    only a bag of lexemes, so one document holding both forms is normal, and the
    query is parsed both ways and OR'd.

    `english` was already this codebase's choice for `about_me` — see
    `ix_user_profiles_about_fts`, built in M2 "deferred from M1" for exactly this
    feature and never queried since. This document supersedes rather than uses it:
    an expression index only serves a query whose expression matches it exactly,
    and a concatenation does not. It becomes droppable when the stored column
    below arrives.

    **Weights are here from the start**, because adding them later reorders every
    result anybody has seen. Postgres scores `{D: 0.1, C: 0.2, B: 0.4, A: 1.0}`,
    so a bio match carries a tenth of a name match — enough to surface a mentor
    nothing else would find, never enough to outrank someone actually called what
    was typed.

    | Weight | Fields |
    |---|---|
    | A | first and last name |
    | B | headline, primary study programme |
    | C | school names, study and origin country |
    | D | bio |

    **Computed inline, and that is the whole scaling story.** This expression
    cannot use an index — it is built per row, per query — so search is a
    sequential scan by construction. Moved into a stored column with a GIN index
    it returns *the same rows in the same order*, because it is the same function
    over the same text. The escalation is therefore a pure performance change with
    no contract in it, which is why starting here is not a shortcut.

    **Measured — and the measurement's own reliability is the first finding.**
    On the development machine this statement's absolute timing varies about
    fivefold with load: the *same unchanged* query measured 50ms at 500 mentors
    in one session and 285ms an hour later, and at 50,000 it returned 6.4s, 11.0s,
    13.0s, 23.8s and 6.2s across runs. **Figures taken in different sessions are
    not comparable**, which is exactly the mistake the earlier version of this
    docstring made — it reported a "before" and an "after" measured an hour apart
    and attributed the difference to the code.

    So the numbers worth keeping are *deltas measured back to back*, and one
    internally-consistent curve for shape:

        500 mentors      ~275 ms
        5,000 mentors  ~3,900 ms

    Linear, as a per-row build must be. D19's escalation line is a p95 of ~200ms,
    which this crosses somewhere between **500 and 2,000 mentors** on this box —
    the range rather than a point, because a fivefold machine swing is wider than
    the interval a single figure would imply, and production hardware is not this
    laptop. Either way it is far nearer than the ~10,000 D19 predicts for a
    well-indexed join, so search is the first thing here that will need it. At
    today's 44 it is milliseconds.

    **What each addition actually cost, A/B in one process:** `study_course`
    adds ~16% at 5,000 mentors and nothing measurable at 500. The card's
    completed-session count costs nothing per row at all — it runs *after* the
    page limit, `loops=21` in the plan, on a partial index. The per-row work is
    this document and the qualification lateral, and only the document is what a
    stored column removes.
    """
    education = (
        select(
            func.string_agg(
                func.coalesce(Institution.name, EducationEntry.school_name_raw, "")
                + " "
                # **Both course and programme, and they are not the same field.**
                # `study_course` is what a mentee means by a subject —
                # "Mathematics", "Physics" — and it is what the card prints;
                # `study_program` holds degree *names* like "BSc (Bachelor of
                # Science)". The document indexed only the latter, so the word
                # displayed on every card found nobody, and every existing search
                # test missed it by searching a name, a school or a country.
                # Added rather than swapped: 8 export rows carry a programme and
                # "Bachelor of Engineering" is a real query.
                + func.coalesce(EducationEntry.study_course, "")
                + " "
                + func.coalesce(EducationEntry.study_program, ""),
                " ",
            )
        )
        .select_from(EducationEntry)
        .outerjoin(Institution, Institution.id == EducationEntry.institution_id)
        .where(
            EducationEntry.user_id == MentorProfile.user_id,
            # Without this a mentor stays findable by a school they deleted. It
            # is the sixth soft-delete of this milestone and the only one in a
            # subquery, where nothing else in the diff would show it.
            EducationEntry.deleted_at.is_(None),
        )
        .correlate(MentorProfile)
        .scalar_subquery()
    )

    def weight(vector: Any, label: str) -> Any:
        """`setweight` takes Postgres's internal `"char"`, not `varchar`.

        Bound as a parameter it arrives as `character varying` and the function
        does not resolve — `setweight(tsvector, character varying) does not
        exist`. An inline literal is left untyped for Postgres to resolve, which
        is the one place in this module where a value is not a bind parameter,
        and it is safe because the label is ours and never user input.
        """
        return func.setweight(vector, literal_column(f"'{label}'"))

    def simple(column: Any) -> Any:
        return func.to_tsvector(SIMPLE, func.coalesce(column, ""))

    def english(column: Any) -> Any:
        return func.to_tsvector(ENGLISH, func.coalesce(column, ""))

    return (
        weight(simple(User.first_name + literal(" ") + User.last_name), "A")
        .op("||")(weight(english(MentorProfile.headline), "B"))
        .op("||")(weight(english(MentorProfile.primary_study_program), "B"))
        .op("||")(weight(simple(education), "C"))
        .op("||")(weight(simple(_STUDY_COUNTRY.c.display_name), "C"))
        .op("||")(weight(simple(_ORIGIN_COUNTRY.c.display_name), "C"))
        .op("||")(weight(english(UserProfile.about_me), "D"))
    )


def _matches(term: str) -> Any:
    """The query, parsed under both configurations and OR'd.

    The document holds `english` lexemes for its prose and `simple` ones for its
    names, so a single parse would only ever reach half of it. `websearch_to_tsquery`
    rather than `to_tsquery` because it never raises on user input — quotes,
    operators and stray punctuation are handled rather than becoming a 500.
    """
    return func.websearch_to_tsquery(SIMPLE, term).op("||")(
        func.websearch_to_tsquery(ENGLISH, term)
    )


def _completed_sessions() -> Any:
    """How many sessions this mentor has delivered.

    Derived, never stored — D56, and the migration package agrees: it lists
    `countCompletedSession` and `percentageOfCompletedSession` on *Mentor (front
    search)*, this exact card, and drops both as "DERIVED at query time".

    **`completed` only.** `no_show` is its own status and stays out: a session
    the mentee never arrived at held the mentor's time and delivered nothing.
    That nuance is not lost, it is somewhere better — per-party attendance on
    `session_participants`, which is what a profile's attendance figure reads.

    Scoped to `mentor_id`, because a mentor is also somebody's mentee and
    sessions they *received* are not sessions they gave. Served by
    `ix_sessions_mentor_completed`, a partial index that has existed since the M4
    schema and until now had no reader.
    """
    return (
        select(func.count())
        .select_from(Session)
        # `delivered()` rather than the predicate inline: the profile shows this
        # same number, and two copies of "what counts as delivered" is the defect
        # #8 describes rather than a style question.
        .where(delivered(MentorProfile.user_id))
        .correlate(MentorProfile)
        .scalar_subquery()
    )


def _base() -> Select[Any]:
    """The columns and the scope, shared by both modes.

    Extracted so browse and search cannot drift on *who is visible*. The two
    differ only in ordering and how they page; if the predicates lived in each,
    a clause added to one would silently not apply to the other — and the one
    with a text box in front of it is the worse half to forget.
    """
    qualification = top_qualification(MentorProfile.user_id)
    # Two columns from one set of rows, so a lateral rather than two scalar
    # subqueries — the same call `_top_qualification` makes, for the same
    # reason. Narrow on purpose: `ix_reviews_mentor_valuable` covers exactly
    # these two under `published()`, so the card stays an index-only scan.
    reviews = card_summary(MentorProfile.user_id).lateral("review_summary")
    return (
        select(
            User.id.label("user_id"),
            MentorProfile.id.label("cursor_id"),
            User.slug,
            User.first_name,
            User.last_name,
            MentorProfile.headline,
            UserProfile.avatar_url,
            _STUDY_COUNTRY.c.display_name.label("primary_study_country"),
            _ORIGIN_COUNTRY.c.display_name.label("origin_country"),
            qualification.c.degree,
            qualification.c.study_course,
            qualification.c.institution,
            _completed_sessions().label("completed_sessions"),
            reviews.c.review_count,
            reviews.c.session_value,
        )
        .select_from(MentorProfile)
        .join(User, User.id == MentorProfile.user_id)
        # Outer: a mentor who never wrote a bio has no `user_profiles` row at all,
        # and an inner join would make them unfindable while their profile page
        # works perfectly — invisible in the one place a mentee looks.
        .outerjoin(UserProfile, UserProfile.user_id == MentorProfile.user_id)
        .outerjoin(_STUDY_COUNTRY, _STUDY_COUNTRY.c.id == MentorProfile.primary_study_country_id)
        # Moved here from `_ranked`, where a comment used to say browse never
        # reads it. Browse does now: where a mentor is *from* is on the card,
        # and it was the one field the search document indexed while the
        # payload withheld it — findable by a fact a client could not display.
        .outerjoin(_ORIGIN_COUNTRY, _ORIGIN_COUNTRY.c.id == UserProfile.origin_country_id)
        # Outer for the same reason: an academic line is something a card
        # *displays*, and a mentor without one is a worse card, not a hidden
        # mentor. Bookability is what decides who appears, and it is above.
        .outerjoin(qualification, true())
        # Outer for the same reason as the qualification: a mentor nobody has
        # reviewed is a card with no rating, not a hidden mentor.
        .outerjoin(reviews, true())
        .where(*mentor_is_public(), *mentor_is_bookable())
    )


def _page(after: UUID | None, limit: int) -> Select[Any]:
    """One page of mentors, newest first.

    `mentor_profiles.id` is both the sort key and the cursor, which is ADR 0016's
    base case — *"the id is the cursor when the display order is the id order"* —
    and it is returned as `cursor_id` rather than left implicit. The row's own
    `id` is the **user**, so the two are different values and the caller must not
    reach for the visible one when building the next token.
    """
    return (
        _base()
        .where(*([MentorProfile.id < after] if after is not None else []))
        .order_by(MentorProfile.id.desc())
        .limit(limit + 1)
    )


def _ranked(term: str, offset: int, limit: int) -> Select[Any]:
    """One page of mentors matching `term`, best first.

    **Offset, not a keyset, and that is deliberate.** A rank is not in the row and
    is not stable: "best match" for a marketplace grows into text relevance plus
    quality signals — rating, completed sessions, response rate — none of which
    exist yet, so the formula *will* change. A cursor encoding a rank is
    invalidated by that change; an offset is not. Every search product pages this
    way for the same reason, and they cap depth rather than solve it.

    `mentor_profiles.id` breaks ties so equal ranks order deterministically.
    Without it two mentors scoring the same could swap between pages and one would
    be shown twice while the other vanished.
    """
    rank = func.ts_rank_cd(_document(), _matches(term))
    return (
        _base()
        .add_columns(rank.label("rank"))
        .where(_document().op("@@")(_matches(term)))
        .order_by(rank.desc(), MentorProfile.id.desc())
        .offset(offset)
        .limit(limit + 1)
    )


async def search_mentors(
    session: AsyncSession,
    *,
    limit: int,
    after: UUID | None = None,
    q: str | None = None,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], bool]:
    """One page of bookable mentors, and whether another follows.

    **Two modes behind one signature.** Without `q` this is a browse list, newest
    first, keyset-paged on `mentor_profiles.id`. With `q` it is a ranked search,
    best first, offset-paged. Both read the same scope from `_base()`, so a
    visibility clause cannot apply to one and not the other.

    A blank `q` is browse, not an empty search — an empty box is the resting
    state of a search field, and answering it with nothing would make the page
    look broken before anybody typed. A `q` that *parses* to nothing is
    different: `websearch_to_tsquery` yields an empty query for input like "and",
    and that legitimately matches no one.

    Two statements either way. The offerings are fetched for the whole page in a
    single query and attached afterwards — written per row it was twenty round
    trips a page.

    One more row than asked for is fetched: if it comes back there is a next
    page. Cheaper and more honest than a second `COUNT`, which can disagree with
    the page it claims to describe.
    """
    # `q` arrives normalised: a blank search is `None` by the time it gets here.
    # Re-deciding would be a second copy of the rule, and the copy that drifts is
    # the one nobody is looking at.
    statement = _ranked(q, offset, limit) if q is not None else _page(after, limit)

    rows = [dict(r) for r in (await session.execute(statement)).mappings()]
    page, has_more = rows[:limit], len(rows) > limit

    grouped = await offerings_for(session, [row["user_id"] for row in page])
    for row in page:
        row["offerings"] = grouped.get(row["user_id"], [])
    return page, has_more
