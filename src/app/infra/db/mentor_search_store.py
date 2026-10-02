"""Finding a mentor — the only endpoint that answers "who is there at all".

Three public reads existed before this and every one of them needed an id or a
slug you already had. This is the one that hands them out.

**Visible, never available.** The scope is `mentor_is_live()`: approved,
listed and not deleted either way — set up to be booked or not, since #219;
each card says which in `taking_bookings`. It says nothing about *when*,
because availability is a computation over projected
windows minus bookings and cannot be a `WHERE` clause. Filtering on it would
mean computing slots for every candidate before paging, which stops the cursor
being a database keyset; and caching it in a column is the drift D20 rejected,
where a stored `is_available` was wrong the moment somebody booked. *When* is
what `/slots` answers, freshly, one click later.

**One filter: service offering, any of.** A mentor who gives *at least one* of
the slugs asked for appears, because chips in one category widen — a mentee who
picks two kinds of help wants mentors for either. It applies in `_who()`, so
browse and search cannot disagree about it, and it is an `EXISTS` because
`mentor_service_offerings` is one-to-many and a join would list a mentor once per
matching slug.

School, degree, country of study and country of origin are still to come. They
were held back because a query parameter is additive and a sort order is not
(rule #21). Three of those four read through `education_entries`, also
one-to-many, so they arrive as `EXISTS` clauses too.

**Ordered by `mentor_profiles.id`, not `users.id`.** Both are UUIDv7 and both are
therefore time-ordered, but they order different events: when somebody signed up
against when they became a mentor. A mentee of two years who started mentoring
last week is a new mentor, and this list is of mentors.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from functools import reduce
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Select,
    Text,
    and_,
    case,
    cast,
    func,
    literal,
    literal_column,
    or_,
    select,
    true,
    tuple_,
)
from sqlalchemy.dialects.postgresql import TSQUERY
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.availability import BookingWindow
from app.domain.search import (
    FUZZY_FLOOR,
    fuzzy_terms,
    has_operators,
    prefix_terms,
    search_groups,
)
from app.infra.db.models.availability import MentorNextAvailability
from app.infra.db.models.education import EducationEntry, Institution
from app.infra.db.models.mentoring import MentorProfile
from app.infra.db.models.reference import Country
from app.infra.db.models.sessions import Session
from app.infra.db.models.user import User, UserProfile
from app.infra.db.next_available_store import public_next_available_state
from app.infra.db.offerings import (
    goal_overlap_count,
    offerings_for,
    offers_any,
    shared_offering_count,
)
from app.infra.db.profile_store import top_award
from app.infra.db.public_visibility import mentor_is_live, taking_bookings
from app.infra.db.qualifications import top_qualification
from app.infra.db.review_stats import card_summary
from app.infra.db.session_stats import delivered

__all__ = [
    "SIMILAR_LIMIT",
    "completed_sessions",
    "count_mentors",
    "mentor_card",
    "search_mentors",
    "similar_mentors",
]

#: `english` stems, which is right for prose and wrong for names. Named rather
#: than inlined so the two never drift apart across the document and the query.
SIMPLE = "simple"
ENGLISH = "english"

_STUDY_COUNTRY = Country.__table__.alias("study_country")


#: The mentor's origin country, aliased separately from where they studied.
_ORIGIN_COUNTRY = Country.__table__.alias("origin_country")


def _education_text() -> Any:
    """Every live education entry's school, course and programme, as one string.

    Read by the full-text document and by the near-spelling text alike, so the
    two cannot disagree about what a mentor studied.
    """
    return (
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

    def weight(vector: Any, label: str) -> Any:
        """`setweight` takes Postgres's internal `"char"`, not `varchar`.

        Bound as a parameter it arrives as `character varying` and the function
        does not resolve — `setweight(tsvector, character varying) does not
        exist`. An inline literal is left untyped for Postgres to resolve, which
        is the one place in this module where a value is not a bind parameter,
        and it is safe because the label is ours and never user input.
        """
        return func.setweight(vector, literal_column(f"'{label}'"))

    fields = [*_card_fields(), (UserProfile.about_me, "D", ENGLISH)]
    vectors = [weight(func.to_tsvector(c, func.coalesce(f, "")), w) for f, w, c in fields]
    return reduce(lambda a, b: a.op("||")(b), vectors)


def _card_fields() -> list[tuple[Any, str, str]]:
    """Every searchable field but the bio, as `(text, weight, configuration)`.

    The one list both the full-text document and the near-spelling text read,
    so a field cannot be searchable one way and not the other. The document adds
    the bio; the near tier leaves it out, because long prose is near-similar to
    almost anything.
    """
    return [
        # `concat_ws`, not `+`: both names are nullable, and one null made the
        # whole name unsearchable.
        (func.concat_ws(" ", User.first_name, User.last_name), "A", SIMPLE),
        (MentorProfile.headline, "B", ENGLISH),
        (MentorProfile.primary_study_program, "B", ENGLISH),
        (_education_text(), "C", SIMPLE),
        (_STUDY_COUNTRY.c.display_name, "C", SIMPLE),
        (_ORIGIN_COUNTRY.c.display_name, "C", SIMPLE),
    ]


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


def _fuzzy_text() -> Any:
    """What a near spelling is compared with: the card fields, without the bio."""
    return func.concat_ws(" ", *(field for field, _, _ in _card_fields()))


def _prefix(terms: list[str]) -> Any:
    """Every term as a prefix, each under both configurations, ANDed (#227).

    Per term, so a course indexed `simple` and a word in a prose field indexed
    `english` can both satisfy one query. A term `english` reads as a stop word
    ("at") is dropped: the prose fields never indexed it, so requiring it would
    fail any match there. An empty tsquery is the identity for `&&`.
    """
    parts: list[Any] = []
    for pattern in prefix_terms(terms):
        english = func.to_tsquery(ENGLISH, pattern)
        parts.append(
            case(
                (func.numnode(english) == 0, cast(literal(""), TSQUERY)),
                else_=func.to_tsquery(SIMPLE, pattern).op("||")(english),
            )
        )
    return reduce(lambda a, b: a.op("&&")(b), parts)


def _tiers(term: str) -> list[tuple[Any, Any]]:
    """Each way `term` can match, best first, as `(condition, score)` pairs (#227).

    Exact full-text, then every word as a prefix, then a near spelling — or
    exact alone for a query with a negation or a phrase (`has_operators`). The
    prefix and near tiers are built from sanitised terms (`domain.search`), so
    no query syntax a user types reaches `to_tsquery`; both are bound.
    """
    document = _document()
    tiers = [(document.op("@@")(_matches(term)), func.ts_rank_cd(document, _matches(term)))]
    if has_operators(term):
        return tiers
    groups = search_groups(term)
    if groups:
        query = reduce(lambda a, b: a.op("||")(b), (_prefix(group) for group in groups))
        tiers.append((document.op("@@")(query), func.ts_rank_cd(document, query)))
    near = [n for n in (_near(group, document) for group in groups) if n is not None]
    if near:
        tiers.append(
            (
                or_(*(condition for condition, _ in near)),
                func.greatest(*(case((condition, score), else_=0) for condition, score in near)),
            )
        )
    return tiers


def _near(group: list[str], document: Any) -> tuple[Any, Any] | None:
    """One `or` group as a near spelling, or None when it has no long word.

    Per word, like the other tiers: every long word must clear the floor (a
    single close word must not carry an absent one) and every short word must be
    present as a prefix (`MIT Harvrd` needs MIT). Scored by the weakest word.
    """
    long, short = fuzzy_terms(group)
    if not long:
        return None
    text = _fuzzy_text()
    scores = [func.strict_word_similarity(word, text) for word in long]
    conditions = [score >= FUZZY_FLOOR for score in scores]
    if short:
        # Short words that are all stop words make an empty query, which
        # matches nothing; they are no requirement at all.
        query = _prefix(short)
        conditions.append(or_(func.numnode(query) == 0, document.op("@@")(query)))
    return and_(*conditions), func.least(*scores)


def _search_match(term: str) -> Any:
    """A mentor matches `term` on any tier. The page and the count both read it."""
    return or_(*(condition for condition, _ in _tiers(term)))


def completed_sessions() -> Any:
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


def _who(offerings: Sequence[str], viewer: UUID | None = None) -> Select[Any]:
    """Which mentors are visible — the joins and predicates, and no card.

    Extracted so browse, search and the count cannot drift on *who is visible*.
    The modes differ only in ordering and how they page; if the predicates lived
    in each, a clause added to one would silently not apply to the other — and
    the one with a text box in front of it is the worse half to forget.

    **Only the joins a predicate or the search document reads.** The card's
    qualification lateral is added by `_card()` instead: it is an ordered
    `LIMIT 1` per mentor that Postgres cannot drop from a query that never reads
    it, so carrying it here made `count_mentors` run it for every visible mentor
    where a page runs it for twenty-one.
    """
    return (
        select(MentorProfile.id)
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
        .where(*mentor_is_live())
        .where(*([offers_any(MentorProfile.user_id, offerings)] if offerings else []))
        # **A signed-in mentor is never listed to themself.** Here rather than
        # in one mode, so browse, the goal ranking, search and `total` all
        # agree — a count that still included the viewer would promise a
        # mentor the pages never show.
        .where(*([MentorProfile.user_id != viewer] if viewer is not None else []))
    )


def _card(scope: Select[Any], window: BookingWindow) -> Select[Any]:
    """`scope`'s mentors with the columns a search result renders."""
    qualification = top_qualification(MentorProfile.user_id)
    # **Scalar subqueries, not a lateral.** They run per output row, after the
    # page limit, which is the property `_document()` records for the
    # completed-session count. A lateral would join in the FROM and run once
    # per *match* on the search path, where the rank sort materialises them all.
    review_count, session_value = card_summary(MentorProfile.user_id)
    return (
        scope.with_only_columns(
            User.id.label("user_id"),
            MentorProfile.id.label("cursor_id"),
            User.slug,
            User.first_name,
            User.last_name,
            MentorProfile.headline,
            UserProfile.avatar_url,
            UserProfile.avatar_focus_x,
            UserProfile.avatar_focus_y,
            _STUDY_COUNTRY.c.display_name.label("primary_study_country"),
            _ORIGIN_COUNTRY.c.display_name.label("origin_country"),
            qualification.c.degree,
            qualification.c.study_course,
            qualification.c.institution,
            completed_sessions().label("completed_sessions"),
            review_count.scalar_subquery().label("review_count"),
            session_value.scalar_subquery().label("session_value"),
            MentorNextAvailability.next_available_at,
            MentorNextAvailability.next_available_session_type_id,
            # Both gated on `taking_bookings()`: a listed mentor nobody can book
            # reads `none`, never a stale or never-refreshed time.
            public_next_available_state(window).label("next_available_state"),
            taking_bookings().label("taking_bookings"),
            # When they became a mentor; backfilled from the legacy platform.
            MentorProfile.created_at.label("joined_at"),
            # Per row, after the page limit, like the review subqueries above.
            top_award(MentorProfile.user_id).label("top_award"),
        )
        # Outer: a mentor the job has not reached yet has no row, and reads as
        # `refreshing` rather than disappearing.
        .outerjoin(
            MentorNextAvailability,
            MentorNextAvailability.mentor_user_id == MentorProfile.user_id,
        )
        # Outer: an academic line is something a card *displays*, and a mentor
        # without one is a worse card, not a hidden mentor. Bookability is what
        # decides who appears, and it is in `_who()`.
        .outerjoin(qualification, true())
    )


def _bookable_first() -> Any:
    """The leading sort key of every Explore order (#220): mentors taking
    bookings before those who are not, by the one `taking_bookings()` rule."""
    return taking_bookings().desc()


def _page(
    after: tuple[bool, UUID] | None,
    limit: int,
    offerings: Sequence[str],
    window: BookingWindow,
    viewer: UUID | None = None,
    offset: int = 0,
) -> Select[Any]:
    """One page of mentors, bookable first, then newest first (#220).

    The cursor is the group and `mentor_profiles.id`, ADR 0016's two-part form,
    returned as `taking_bookings` and `cursor_id` rather than left implicit. The row's own
    `id` is the **user**, so the two are different values and the caller must not
    reach for the visible one when building the next token.
    """
    return (
        _card(_scope(None, offerings, viewer), window)
        .where(
            *(
                [
                    tuple_(taking_bookings(), MentorProfile.id)
                    < tuple_(literal(after[0]), literal(after[1]))
                ]
                if after is not None
                else []
            )
        )
        .order_by(_bookable_first(), MentorProfile.id.desc())
        # Only ever non-zero when a goal-ranked page's offset cursor comes back
        # for a viewer who no longer has goals (a token that lapsed, or a last
        # goal removed): newest first from that position, so paging goes on —
        # a row may repeat or be skipped across the switch, never a 422.
        .offset(offset)
        .limit(limit + 1)
    )


def _scope(term: str | None, offerings: Sequence[str], viewer: UUID | None = None) -> Select[Any]:
    """Who a request lists, before any ordering or paging.

    The page and the count both read this, so `total` cannot describe a
    different set from the pages it sits beside — a count that forgot the text
    match or the filter would still return a plausible number.
    """
    base = _who(offerings, viewer)
    return base if term is None else base.where(_search_match(term))


def _matched(
    viewer: UUID,
    day: dt.date,
    offset: int,
    limit: int,
    offerings: Sequence[str],
    window: BookingWindow,
) -> Select[Any]:
    """One page of mentors for a mentee with goals: most goals covered first.

    **Ties are shuffled, fixed per viewer per day.** The shuffle key is
    `md5(viewer : day : mentor)`, so one mentee sees one order all day — paging
    is stable and a refresh does not reshuffle — while tomorrow, and every other
    mentee, sees the tied mentors in a different order. Without it every mentee
    covering the same goals would see the same newest mentors first, forever.

    **Offset-paged, like search**, for the reason `_ranked` gives: the order is
    not a column in the row. `mentor_profiles.id` is the last tie-break, so even
    a hash collision cannot make the order non-deterministic.
    """
    overlap = goal_overlap_count(MentorProfile.user_id, viewer)
    shuffle = func.md5(literal(f"{viewer}:{day.isoformat()}:").concat(cast(MentorProfile.id, Text)))
    return (
        _card(_scope(None, offerings, viewer), window)
        .order_by(_bookable_first(), overlap.desc(), shuffle, MentorProfile.id.desc())
        .offset(offset)
        .limit(limit + 1)
    )


def _ranked(
    term: str,
    offset: int,
    limit: int,
    offerings: Sequence[str],
    window: BookingWindow,
    viewer: UUID | None = None,
) -> Select[Any]:
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
    tiers = _tiers(term)
    # The tier (exact, prefix, near) orders before the score within it, so a
    # strong prefix match never outranks a weak exact one (#227).
    tier = case(*((condition, len(tiers) - n) for n, (condition, _) in enumerate(tiers)), else_=0)
    rank = case(*((condition, score) for condition, score in tiers), else_=0)
    return (
        _card(_scope(term, offerings, viewer), window)
        .add_columns(rank.label("rank"))
        .order_by(_bookable_first(), tier.desc(), rank.desc(), MentorProfile.id.desc())
        .offset(offset)
        .limit(limit + 1)
    )


async def search_mentors(
    session: AsyncSession,
    *,
    limit: int,
    after: tuple[bool, UUID] | None = None,
    q: str | None = None,
    offset: int = 0,
    offerings: Sequence[str] = (),
    viewer: UUID | None = None,
    goal_day: dt.date | None = None,
    window: BookingWindow,
) -> tuple[list[dict[str, Any]], bool]:
    """One page of listed mentors, and whether another follows.

    **`viewer` is who is asking**, and never appears in their own list.
    **`goal_day` switches browse to the goal ranking** (`_matched`) for that
    viewer and day; the caller passes it only for a viewer who has goals, so a
    mentee without any browses bookable first, then newest (#220). `q` outranks
    both: a search is ranked by the search.

    **Two modes behind one signature.** Without `q` this is a browse list, newest
    first, keyset-paged on `mentor_profiles.id`. With `q` it is a ranked search,
    best first, offset-paged. Both read the same scope from `_who()`, so a
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
    page. *Whether there is more* never reads the count — a `COUNT` can
    disagree with the page it sits beside under concurrent writes. The header's
    number is `count_mentors`, a separate, display-only answer over the same
    `_scope()`.
    """
    # `q` arrives normalised: a blank search is `None` by the time it gets here.
    # Re-deciding would be a second copy of the rule, and the copy that drifts is
    # the one nobody is looking at.
    #
    # `offerings` arrives validated as live slugs; the store only applies them.
    if q is not None:
        statement = _ranked(q, offset, limit, offerings, window, viewer)
    elif viewer is not None and goal_day is not None:
        statement = _matched(viewer, goal_day, offset, limit, offerings, window)
    else:
        statement = _page(after, limit, offerings, window, viewer, offset)

    rows = [dict(r) for r in (await session.execute(statement)).mappings()]
    page, has_more = rows[:limit], len(rows) > limit

    grouped = await offerings_for(session, [row["user_id"] for row in page])
    for row in page:
        row["offerings"] = grouped.get(row["user_id"], [])
    return page, has_more


async def count_mentors(
    session: AsyncSession,
    *,
    q: str | None = None,
    offerings: Sequence[str] = (),
    viewer: UUID | None = None,
) -> int:
    """How many mentors `search_mentors` would list across every page.

    **A second statement, and that is the cost this field carries.** On the
    search path it is a second sequential scan over the inline document, so
    `total` roughly doubles what a first search page costs; browse is a count
    over the visibility predicates alone. The
    route asks for it on the first page only, so paging does not pay it again.

    Reads `_scope()` without `_card()`, so none of the card's per-row work — the
    qualification lateral, the review and session subqueries — runs per mentor
    counted.
    """
    statement = _scope(q, offerings, viewer).with_only_columns(func.count(MentorProfile.id))
    return int((await session.execute(statement)).scalar_one())


async def mentor_card(
    session: AsyncSession, user_id: UUID, *, window: BookingWindow
) -> dict[str, Any] | None:
    """One visible mentor as a discovery card, with their bio.

    **The same `_card()` over the same `_scope()`** the list reads, so a card
    shown on its own — the featured mentor — cannot differ from the same mentor
    in the list. `None` when the mentor is not visible. Being bookable is the
    caller's check: the featured rotation picks only from `bookable_mentors()`.
    """
    statement = (
        _card(_scope(None, ()), window)
        .add_columns(UserProfile.about_me)
        .where(MentorProfile.user_id == user_id)
    )
    row = (await session.execute(statement)).mappings().first()
    if row is None:
        return None
    card = dict(row)
    card["offerings"] = (await offerings_for(session, [user_id])).get(user_id, [])
    return card


#: At most this many similar mentors — a row under a profile, not a list.
SIMILAR_LIMIT = 3


async def similar_mentors(
    session: AsyncSession,
    mentor_user_id: UUID,
    *,
    window: BookingWindow,
    limit: int = SIMILAR_LIMIT,
) -> list[dict[str, Any]]:
    """Bookable mentors who give the same kind of help as this one, best first.

    **Similar means sharing a service offering** — the closed taxonomy matching
    already runs on — and each card says which one (`shared_offering`), so the
    suggestion explains itself.

    **Candidates are exactly who discovery lists**: `_card(_scope(...))`, the
    same visibility (#219), narrowed by the same `offers_any` filter
    `?offering=` uses. A suggestion that links to a 404 is worse than none; one
    not taking bookings is shown, and its card says so in `taking_bookings`.

    **Ranked by how many offerings are shared, then by delivered sessions, then
    by review count** — the two proofs of a working mentor the card already
    shows — and finally by `mentor_profiles.id`, newest first, so the order is
    total and a refresh never reshuffles a tie.

    `shared_offering` is the first shared offering in the platform's own order:
    `offerings_for` returns each list in `sort_order`, so the first candidate
    offering that this mentor also gives is it. Two statements beyond the
    mentor's own offerings: the ranked cards, and one batch for their offerings.
    """
    mine = [
        o["slug"] for o in (await offerings_for(session, [mentor_user_id])).get(mentor_user_id, [])
    ]
    if not mine:
        return []

    shared = shared_offering_count(MentorProfile.user_id, mine).label("shared_offerings")
    statement = (
        _card(_scope(None, mine), window)
        .add_columns(shared)
        .where(MentorProfile.user_id != mentor_user_id)
        .order_by(
            shared.desc(),
            literal_column("completed_sessions").desc(),
            literal_column("review_count").desc(),
            MentorProfile.id.desc(),
        )
        .limit(limit)
    )
    rows = [dict(r) for r in (await session.execute(statement)).mappings()]

    grouped = await offerings_for(session, [row["user_id"] for row in rows])
    wanted = set(mine)
    for row in rows:
        row["offerings"] = grouped.get(row["user_id"], [])
        # `None` only if an offering was retired between the two statements;
        # such a row no longer has a reason to be here, so it is dropped.
        row["shared_offering"] = next((o for o in row["offerings"] if o["slug"] in wanted), None)
    return [row for row in rows if row["shared_offering"] is not None]
