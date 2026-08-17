"""Loading the seven profile tables.

Order is not alphabetical and is not negotiable: ``mentor_profiles`` and
``mentee_goals`` come first because the three junctions reference *them* — by
``user_id``, not by ``users.id`` — which is what makes it structurally impossible
to attach a mentor-only row to a mentee. A junction written before its parent
fails on the foreign key, which is the right failure but a confusing one.

All seven run inside the caller's transaction and inside
``timestamps_from_source``, so Bubble's ``created_at``/``updated_at`` survive the
way they do for ``users`` — and a failure rolls the whole phase back together
rather than leaving five tables loaded and two not.

**``predicates.LIVE`` is deliberately absent**, matching ``satellites.py``. It
guards stores that read or update an *existing* user; every statement here
creates a row, during a freeze in which nothing is soft-deleted, and it is a
SQLAlchemy expression that cannot compose into a ``text()`` statement regardless.
The parity test walks the two stores that do own it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain.enums import ApprovalStatus
from app.domain.resolve import Resolution
from app.domain.transform.profiles import (
    AwardRow,
    EducationRow,
    MenteeGoalRow,
    MentorProfileRow,
)

# One event per migrated mentor, and only if they have none.
#
# **The status columns above are still written directly, and this is the one
# place that happens.** The loader syncs current state from a re-exported
# snapshot, so a re-run must be able to correct it; going through the log
# instead would append a second event every time. The guard keeps the history
# itself idempotent.
#
# `created_at` comes from the source decision where Bubble recorded one, and
# from the profile otherwise — the same honesty the migration's backfill uses.
# `created_by` is null: nobody in this system made these decisions.
SEED_MENTOR_HISTORY = """
INSERT INTO mentor_status_events (mentor_user_id, status_type, reason, created_at)
VALUES (:user_id, :status_type, :reason, :created_at)
"""

#: Whether this mentor already has a history, asked **once** before writing both
#: events.
#:
#: A `WHERE NOT EXISTS` on each insert looks equivalent and is not: the first
#: event satisfies the second one's guard, so only the approval was ever
#: written and every migrated mentor lost their listing event. Caught by a test
#: asserting the reason, not by reading the SQL.
MENTOR_HAS_HISTORY = """
SELECT 1 FROM mentor_status_events WHERE mentor_user_id = :user_id LIMIT 1
"""

UPSERT_MENTOR_PROFILE = """
INSERT INTO mentor_profiles (
    user_id, legacy_bubble_id, created_at, updated_at,
    approval_status, listing_status, requires_booking_confirmation,
    primary_study_country_id, primary_study_program
) VALUES (
    :user_id, :legacy_bubble_id, :created_at, :updated_at,
    :approval_status,
    :listing_status,
    :requires_booking_confirmation,
    :primary_study_country_id, :primary_study_program
)
ON CONFLICT (legacy_bubble_id) DO UPDATE SET
    user_id                       = EXCLUDED.user_id,
    created_at                    = EXCLUDED.created_at,
    updated_at                    = EXCLUDED.updated_at,
    approval_status               = EXCLUDED.approval_status,
    listing_status                = EXCLUDED.listing_status,
    requires_booking_confirmation = EXCLUDED.requires_booking_confirmation,
    primary_study_country_id      = EXCLUDED.primary_study_country_id,
    primary_study_program         = EXCLUDED.primary_study_program
"""

UPSERT_GOAL = """
INSERT INTO mentee_goals (
    user_id, legacy_bubble_id, created_at, updated_at, degree_goal_id, degree_goal_raw
) VALUES (
    :user_id, :legacy_bubble_id, :created_at, :updated_at, :degree_goal_id, :degree_goal_raw
)
ON CONFLICT (legacy_bubble_id) DO UPDATE SET
    user_id         = EXCLUDED.user_id,
    created_at      = EXCLUDED.created_at,
    updated_at      = EXCLUDED.updated_at,
    degree_goal_id  = EXCLUDED.degree_goal_id,
    degree_goal_raw = EXCLUDED.degree_goal_raw
"""

UPSERT_EDUCATION = """
INSERT INTO education_entries (
    user_id, legacy_bubble_id, created_at, updated_at, school_name_raw,
    degree_category, degree_level_id, degree_abbreviation, study_course, study_program,
    date_start, date_end, is_most_recent
) VALUES (
    :user_id, :legacy_bubble_id, :created_at, :updated_at, :school_name_raw,
    :degree_category, :degree_level_id, :degree_abbreviation, :study_course, :study_program,
    :date_start, :date_end, :is_most_recent
)
ON CONFLICT (legacy_bubble_id) DO UPDATE SET
    user_id         = EXCLUDED.user_id,
    created_at      = EXCLUDED.created_at,
    updated_at      = EXCLUDED.updated_at,
    school_name_raw = EXCLUDED.school_name_raw,
    degree_category = EXCLUDED.degree_category,
    degree_level_id = EXCLUDED.degree_level_id,
    degree_abbreviation = EXCLUDED.degree_abbreviation,
    study_course    = EXCLUDED.study_course,
    study_program   = EXCLUDED.study_program,
    date_start      = EXCLUDED.date_start,
    date_end        = EXCLUDED.date_end,
    is_most_recent  = EXCLUDED.is_most_recent
"""

# `institution_id` is deliberately not written. Institutions are matched in a
# separate, re-runnable pass (ADR 0008) — the link is an UPDATE against these
# rows, so it can be retuned without reloading anything.
UPSERT_AWARD = """
INSERT INTO user_awards (
    user_id, legacy_bubble_id, created_at, updated_at, institution, title, year
) VALUES (
    :user_id, :legacy_bubble_id, :created_at, :updated_at, :institution, :title, :year
)
ON CONFLICT (legacy_bubble_id) DO UPDATE SET
    user_id     = EXCLUDED.user_id,
    created_at  = EXCLUDED.created_at,
    updated_at  = EXCLUDED.updated_at,
    institution = EXCLUDED.institution,
    title       = EXCLUDED.title,
    year        = EXCLUDED.year
"""

# The three junctions carry no `legacy_bubble_id` — each is derived from a list
# column of its parent Thing rather than from a Bubble Thing of its own (settled
# decision #27), so the natural pair is the idempotency key.
UPSERT_MENTOR_SERVICE = """
INSERT INTO mentor_service_offerings (mentor_user_id, service_offering_id)
VALUES (:user_id, :service_offering_id)
ON CONFLICT (mentor_user_id, service_offering_id) DO NOTHING
"""

UPSERT_GOAL_NEED = """
INSERT INTO mentee_goal_needs (user_id, service_offering_id)
VALUES (:user_id, :service_offering_id)
ON CONFLICT (user_id, service_offering_id) DO NOTHING
"""

UPSERT_GOAL_COUNTRY = """
INSERT INTO mentee_goal_countries (user_id, country_id, priority)
VALUES (:user_id, :country_id, :priority)
ON CONFLICT (user_id, country_id) DO UPDATE SET priority = EXCLUDED.priority
"""


@dataclass(frozen=True, slots=True)
class ProfileCounts:
    """What was written, and what could not be.

    The counts are of rows the loader *issued*, which is why the transform
    deduplicates before it gets here: ``ON CONFLICT DO NOTHING`` would absorb a
    repeated offering either way, and a reconciliation that reports two where one
    row exists is worse than no reconciliation at all.
    """

    mentor_profiles: int = 0
    mentor_services: int = 0
    education: int = 0
    awards: int = 0
    goals: int = 0
    goal_countries: int = 0
    goal_needs: int = 0
    countries_skipped: tuple[str, ...] = ()
    empty_tables: tuple[str, ...] = field(default_factory=tuple)


async def lookup_maps(connection: AsyncConnection) -> tuple[dict[str, UUID], dict[str, UUID]]:
    """``service_offerings`` and ``degree_levels``, keyed on slug.

    Queried rather than hard-coded: reference ids are generated per environment
    (ADR 0015), so a literal would be correct in one database and silently wrong
    in every other. The slug is what the transform emits precisely because it is
    the one stable thing — an id differs per database and a display name is free
    to be re-worded.
    """
    offerings = await connection.execute(text("SELECT slug, id FROM service_offerings"))
    degrees = await connection.execute(text("SELECT slug, id FROM degree_levels"))
    return (
        {row.slug: row.id for row in offerings},
        {row.slug: row.id for row in degrees},
    )


class ProfileLoader:
    """The seven profile tables, parents before junctions."""

    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def load(
        self,
        *,
        users: dict[str, UUID],
        mentors: Sequence[MentorProfileRow],
        education: Sequence[EducationRow],
        awards: Sequence[AwardRow],
        goals: Sequence[MenteeGoalRow],
        offerings: dict[str, UUID],
        degrees: dict[str, UUID],
        countries: Resolution,
    ) -> ProfileCounts:
        def user_id(bubble_id: str, what: str) -> UUID:
            resolved = users.get(bubble_id)
            if resolved is None:
                # Cannot happen if the identity load ran first and reconciled, and
                # a foreign key would catch it anyway. Skipping silently is what
                # must not happen, so this raises.
                raise LookupError(f"{what} references unknown user {bubble_id}")
            return resolved

        skipped_countries: set[str] = set()

        mentor_count = 0
        mentor_service_count = 0
        for mentor in mentors:
            # `user_bubble_id`, not `legacy_bubble_id`. The row carries both, and
            # they are the same shape: the front-search row's own id and the id
            # of the user it belongs to. Getting it wrong here raised on the
            # first real load rather than attaching every mentor profile to
            # nobody — which is the whole reason this lookup refuses instead of
            # skipping.
            owner = user_id(mentor.user_bubble_id, "mentor profile")
            country_id = None
            if mentor.primary_study_country_name:
                if mentor.primary_study_country_name in countries:
                    country_id = countries[mentor.primary_study_country_name]
                else:
                    # The profile still loads without it. A country that did not
                    # resolve is reported by name so somebody can add an alias;
                    # dropping the mentor over it would lose the approval state.
                    skipped_countries.add(mentor.primary_study_country_name)

            await self._connection.execute(
                text(UPSERT_MENTOR_PROFILE),
                {
                    "user_id": owner,
                    "legacy_bubble_id": mentor.legacy_bubble_id,
                    "created_at": mentor.created_at,
                    "updated_at": mentor.updated_at,
                    "approval_status": mentor.approval_status.value,
                    "listing_status": mentor.listing_status.value,
                    # Back on this row, and this loader is its only ETL writer.
                    # The session loader must not also write it: two writers for
                    # one fact is what D88 produced in the other direction.
                    # There is no venue here and nowhere for one to go — venue is
                    # per offering and travels on `SessionTypeRow`.
                    "requires_booking_confirmation": mentor.requires_booking_confirmation,
                    "primary_study_country_id": country_id,
                    "primary_study_program": mentor.primary_study_program,
                },
            )
            already = await self._connection.execute(text(MENTOR_HAS_HISTORY), {"user_id": owner})
            # **A pending mentor gets no events, and that is not an enum detail.**
            # `mentor_status_type` has no `pending` member, so seeding one raised
            # `InvalidTextRepresentationError` and took the whole single-transaction
            # load down with it — for the *default* state, which most of the export
            # is in. But the enum is downstream of the rule: the migration's
            # backfill already excludes these mentors, twice, with
            # `WHERE approval_status <> 'pending'`, because a decision row for a
            # mentor nobody has decided on fabricates a decision nobody made.
            #
            # Skipped on the **mentor**, not per event. Filtering to values the
            # enum happens to accept looks equivalent and is not: a pending
            # mentor's `listing_status` is `unlisted`, which *is* a member, so
            # that version would seed a listing event the backfill does not — and
            # the two writers of this table would disagree depending on whether a
            # mentor arrived by migration or by ETL, with nothing reporting it.
            #
            # The pending condition sits beside the has-history one rather than
            # replacing it: a mentor decided in a later export must gain the
            # history this run correctly withheld.
            undecided = mentor.approval_status is ApprovalStatus.PENDING
            seeded = (
                ()
                if already.first() is not None or undecided
                else (
                    (mentor.approval_status.value, None, mentor.approved_at or mentor.created_at),
                    (
                        mentor.listing_status.value,
                        mentor.unlisted_reason.value if mentor.unlisted_reason else None,
                        mentor.created_at,
                    ),
                )
            )
            for status_type, reason, when in seeded:
                await self._connection.execute(
                    text(SEED_MENTOR_HISTORY),
                    {
                        "user_id": owner,
                        "status_type": status_type,
                        "reason": reason,
                        "created_at": when,
                    },
                )
            mentor_count += 1

            for slug in mentor.service_slugs:
                await self._connection.execute(
                    text(UPSERT_MENTOR_SERVICE),
                    {"user_id": owner, "service_offering_id": offerings[slug]},
                )
                mentor_service_count += 1

        goal_count = 0
        country_count = 0
        need_count = 0
        for goal in goals:
            owner = user_id(goal.user_bubble_id, "mentee goals")
            await self._connection.execute(
                text(UPSERT_GOAL),
                {
                    "user_id": owner,
                    "legacy_bubble_id": goal.legacy_bubble_id,
                    "created_at": goal.created_at,
                    "updated_at": goal.updated_at,
                    "degree_goal_id": degrees.get(goal.degree_goal_slug or ""),
                    "degree_goal_raw": goal.degree_goal_raw,
                },
            )
            goal_count += 1

            for index, name in enumerate(goal.country_names, start=1):
                if name not in countries:
                    skipped_countries.add(name)
                    continue
                await self._connection.execute(
                    text(UPSERT_GOAL_COUNTRY),
                    {"user_id": owner, "country_id": countries[name], "priority": index},
                )
                country_count += 1

            for slug in goal.service_slugs:
                await self._connection.execute(
                    text(UPSERT_GOAL_NEED),
                    {"user_id": owner, "service_offering_id": offerings[slug]},
                )
                need_count += 1

        education_count = 0
        for entry in education:
            await self._connection.execute(
                text(UPSERT_EDUCATION),
                {
                    "user_id": user_id(entry.user_bubble_id, "education entry"),
                    "legacy_bubble_id": entry.legacy_bubble_id,
                    "created_at": entry.created_at,
                    "updated_at": entry.updated_at,
                    "school_name_raw": entry.school_name_raw,
                    "degree_category": entry.degree_category,
                    "degree_level_id": degrees.get(entry.degree_level_slug or ""),
                    "degree_abbreviation": entry.degree_abbreviation,
                    "study_course": entry.study_course,
                    "study_program": entry.study_program,
                    "date_start": entry.date_start,
                    "date_end": entry.date_end,
                    "is_most_recent": entry.is_most_recent,
                },
            )
            education_count += 1

        award_count = 0
        for award in awards:
            await self._connection.execute(
                text(UPSERT_AWARD),
                {
                    "user_id": user_id(award.user_bubble_id, "award"),
                    "legacy_bubble_id": award.legacy_bubble_id,
                    "created_at": award.created_at,
                    "updated_at": award.updated_at,
                    "institution": award.institution,
                    "title": award.title,
                    "year": award.year,
                },
            )
            award_count += 1

        counted = {
            "mentor_profiles": mentor_count,
            "mentor_service_offerings": mentor_service_count,
            "education_entries": education_count,
            "user_awards": award_count,
            "mentee_goals": goal_count,
            "mentee_goal_countries": country_count,
            "mentee_goal_needs": need_count,
        }
        return ProfileCounts(
            mentor_profiles=mentor_count,
            mentor_services=mentor_service_count,
            education=education_count,
            awards=award_count,
            goals=goal_count,
            goal_countries=country_count,
            goal_needs=need_count,
            countries_skipped=tuple(sorted(skipped_countries)),
            # Named rather than left to be inferred from a zero. A dev load
            # legitimately writes no goal countries — `Country Goal` is blank on
            # every row — and a report that does not say so reads as coverage.
            empty_tables=tuple(name for name, n in counted.items() if n == 0),
        )
