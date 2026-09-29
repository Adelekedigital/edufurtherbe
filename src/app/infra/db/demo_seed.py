"""Demo mentors for a dev environment: create them, and remove every trace.

**Dev data, never production.** `scripts/seed_demo_mentors.py` refuses a
production environment before it reaches anything here. The rows are ordinary
rows — the point is that the explore page, search, filters, next free time and
featured mentor all show something real through the real API.

**One marker, and removal keys on nothing else.** Every demo user's email ends
in `@demo.edufurther.test` — a reserved `.test` domain no real address can have.
`remove_demo` finds users by that suffix and deletes exactly them, in the order
the `RESTRICT` foreign keys require: reviews and sessions first (their history
is retained on purpose for real users), then the users, whose own rows cascade.

The SQL lives here because `scripts/` may hold none (CLAUDE.md, #44); the roster
— names, schools, counts — is data, and lives in the script.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import ApplicationStage
from app.domain.sessions import first_stage
from app.infra.db.stages import write_session_type_stages

__all__ = [
    "DEMO_DOMAIN",
    "LEGACY_SESSION_TYPE",
    "DemoMentor",
    "DemoQuestion",
    "DemoSessionType",
    "RealHistoryError",
    "apply_demo_session_types",
    "catalogue_offerings",
    "create_demo_mentor",
    "demo_avatar_urls",
    "demo_mentor_offerings",
    "remove_demo",
]

#: The suffix every demo user's email carries, and the only thing removal reads.
DEMO_DOMAIN = "demo.edufurther.test"


#: The one session type every demo mentor was seeded with before the catalogue.
#: An upgrade rewrites this row in place — the demo sessions point at it.
LEGACY_SESSION_TYPE = "1:1 mentorship"


@dataclass(frozen=True, slots=True)
class DemoQuestion:
    """One free-text intake question on a demo session type."""

    text: str
    required: bool = False


@dataclass(frozen=True, slots=True)
class DemoSessionType:
    """One session type as the profile and booking flow will show it."""

    name: str
    description: str
    #: A service-offering slug, or `None` for a type that matches no filter.
    offering: str | None
    duration: int
    #: Minutes; within the write path's 24-72 hour range (#104).
    notice: int
    #: An `ApplicationStage` value, or `None` for any stage.
    stage: str | None
    questions: tuple[DemoQuestion, ...] = ()


@dataclass(frozen=True, slots=True)
class DemoMentor:
    """One demo mentor, as the explore card will show them."""

    key: str
    first_name: str
    last_name: str
    headline: str
    about_me: str
    timezone: str
    study_country: str
    origin_country: str
    degree_level: str
    course: str
    school: str
    offerings: tuple[str, ...]
    completed_sessions: int
    #: One `valuable_rating` per review, each from a different demo mentee.
    ratings: tuple[int, ...] = ()
    #: False blocks the whole booking horizon, so the card reads `none`.
    open: bool = True
    weekdays: tuple[int, ...] = field(default=(0, 1, 2, 3, 4))
    #: What they offer, first one first. Empty keeps the old single
    #: `LEGACY_SESSION_TYPE`, which is what the tests' plain mentors use.
    session_types: tuple[DemoSessionType, ...] = ()


def _email(key: str) -> str:
    return f"{key}@{DEMO_DOMAIN}"


async def _one(session: AsyncSession, sql: str, params: dict[str, Any]) -> Any:
    return (await session.execute(text(sql), params)).scalar_one()


async def create_demo_mentor(
    session: AsyncSession, mentor: DemoMentor, *, now: dt.datetime
) -> UUID:
    """Create one bookable demo mentor with their history. Does not commit."""
    user = await _one(
        session,
        "INSERT INTO users (email, auth_id, first_name, last_name, primary_role, timezone) "
        "VALUES (:e, gen_random_uuid(), :f, :l, 'mentor', :z) RETURNING id",
        {
            "e": _email(mentor.key),
            "f": mentor.first_name,
            "l": mentor.last_name,
            "z": mentor.timezone,
        },
    )
    await session.execute(
        text(
            "INSERT INTO user_profiles (user_id, about_me, origin_country_id) "
            "VALUES (:u, :a, (SELECT id FROM countries WHERE code = :o))"
        ),
        {"u": user, "a": mentor.about_me, "o": mentor.origin_country},
    )
    await session.execute(
        text(
            "INSERT INTO mentor_profiles (user_id, headline, approval_status, listing_status, "
            " primary_study_country_id) "
            "VALUES (:u, :h, 'approved', 'listed', (SELECT id FROM countries WHERE code = :c))"
        ),
        {"u": user, "h": mentor.headline, "c": mentor.study_country},
    )
    await session.execute(
        text(
            "INSERT INTO education_entries "
            "(user_id, school_name_raw, degree_level_id, study_course, date_end) "
            "VALUES (:u, :s, (SELECT id FROM degree_levels WHERE slug = :lvl), :c, :end)"
        ),
        {
            "u": user,
            "s": mentor.school,
            "lvl": mentor.degree_level,
            "c": mentor.course,
            "end": dt.date(now.year - 1, 7, 1),
        },
    )
    for slug in mentor.offerings:
        await session.execute(
            text(
                "INSERT INTO mentor_service_offerings (mentor_user_id, service_offering_id) "
                "SELECT :u, id FROM service_offerings WHERE slug = :s"
            ),
            {"u": user, "s": slug},
        )

    if mentor.session_types:
        session_type = await apply_demo_session_types(session, user, mentor.session_types)
    else:
        session_type = await _one(
            session,
            "INSERT INTO session_types (mentor_user_id, name, service_offering_id, is_active) "
            "VALUES (:u, :n, (SELECT id FROM service_offerings WHERE slug = :s), true) "
            "RETURNING id",
            {"u": user, "n": LEGACY_SESSION_TYPE, "s": mentor.offerings[0]},
        )
        await session.execute(
            text(
                "INSERT INTO session_type_booking_configs "
                "(session_type_id, duration_minutes, min_notice_minutes) VALUES (:t, 45, 1440)"
            ),
            {"t": session_type},
        )
    for day in mentor.weekdays:
        await session.execute(
            text(
                "INSERT INTO availability_rules "
                "(mentor_user_id, day_of_week, start_time, end_time, timezone, is_active) "
                "VALUES (:u, :d, '09:00', '17:00', :z, true)"
            ),
            {"u": user, "d": day, "z": mentor.timezone},
        )
    if not mentor.open:
        # Past the 56-day booking horizon, so nothing is free inside it.
        today = now.date()
        await session.execute(
            text(
                "INSERT INTO availability_exceptions (mentor_user_id, type, date_range, timezone) "
                "VALUES (:u, 'block', daterange(:d, :e), :z)"
            ),
            {"u": user, "d": today, "e": today + dt.timedelta(days=70), "z": mentor.timezone},
        )

    await _history(session, user, session_type, mentor, now=now)
    return UUID(str(user))


async def apply_demo_session_types(
    session: AsyncSession, user: Any, types: tuple[DemoSessionType, ...]
) -> Any:
    """Give a demo mentor exactly these session types; return the first one's id.

    **Safe to run again.** A type is matched by name and rewritten to the
    template, so a second run converges instead of duplicating. **The legacy
    `LEGACY_SESSION_TYPE` row becomes the first type in place**, keeping its id,
    because the demo sessions and reviews point at it. Questions are added only
    to a type that has none, so a re-run never stacks them. Does not commit.
    """
    existing = {
        row.name: row.id
        for row in await session.execute(
            text(
                "SELECT id, name FROM session_types "
                "WHERE mentor_user_id = :u AND deleted_at IS NULL"
            ),
            {"u": user},
        )
    }
    first_id: Any = None
    for index, template in enumerate(types):
        type_id = existing.get(template.name)
        if type_id is None and index == 0:
            type_id = existing.get(LEGACY_SESSION_TYPE)
        # Through the stage set (#215), so the rows the reads prefer and the
        # legacy column agree; the demo's stages never include `other`, so no
        # label is written.
        stages = [ApplicationStage(template.stage)] if template.stage else []
        values = {
            "u": user,
            "n": template.name,
            "d": template.description,
            "o": template.offering,
            "st": first_stage(stages),
        }
        if type_id is None:
            type_id = await _one(
                session,
                "INSERT INTO session_types (mentor_user_id, name, description, "
                " service_offering_id, application_stage, is_active) "
                "VALUES (:u, :n, :d, (SELECT id FROM service_offerings WHERE slug = :o), "
                "        :st, true) "
                "RETURNING id",
                values,
            )
            await session.execute(
                text(
                    "INSERT INTO session_type_booking_configs "
                    "(session_type_id, duration_minutes, min_notice_minutes) VALUES (:t, :m, :n)"
                ),
                {"t": type_id, "m": template.duration, "n": template.notice},
            )
        else:
            await session.execute(
                text(
                    "UPDATE session_types SET name = :n, description = :d, "
                    " service_offering_id = (SELECT id FROM service_offerings WHERE slug = :o), "
                    " application_stage = :st, custom_stage_label = NULL, is_active = true, "
                    # Converges on the template, shown: a schedule a tester left
                    # must go too, or showing it trips the hidden-while-scheduled
                    # `CHECK` and aborts the seed (#218).
                    " deletion_scheduled_at = NULL "
                    "WHERE id = :t"
                ),
                {**values, "t": type_id},
            )
            await session.execute(
                text(
                    "UPDATE session_type_booking_configs "
                    "SET duration_minutes = :m, min_notice_minutes = :n WHERE session_type_id = :t"
                ),
                {"t": type_id, "m": template.duration, "n": template.notice},
            )
        await write_session_type_stages(session, type_id, stages)
        has_questions = await _one(
            session,
            "SELECT EXISTS (SELECT 1 FROM session_type_questions "
            "WHERE session_type_id = :t AND deleted_at IS NULL)",
            {"t": type_id},
        )
        if not has_questions:
            for order, question in enumerate(template.questions):
                await session.execute(
                    text(
                        "INSERT INTO session_type_questions (session_type_id, question_text, "
                        " question_type, is_required, display_order, created_by) "
                        "VALUES (:t, :q, 'free_text', :r, :o, :u)"
                    ),
                    {
                        "t": type_id,
                        "q": question.text,
                        "r": question.required,
                        "o": order,
                        "u": user,
                    },
                )
        first_id = first_id if first_id is not None else type_id
    return first_id


async def demo_mentor_offerings(session: AsyncSession) -> list[tuple[Any, tuple[str, ...]]]:
    """Every demo **mentor** and their offerings, the legacy type's offering first.

    Joined to `mentor_profiles`, because the suffix also matches the demo mentees
    the seed creates to hold its reviews — and they are not mentors.

    The seed made the legacy type from a mentor's first offering, and nothing
    else recorded which one that was — so it is read back from that row.
    """
    rows = await session.execute(
        text(
            "SELECT u.id, "
            "       (SELECT o.slug FROM session_types t JOIN service_offerings o "
            "          ON o.id = t.service_offering_id "
            "        WHERE t.mentor_user_id = u.id AND t.deleted_at IS NULL "
            "        ORDER BY t.created_at LIMIT 1) AS first, "
            "       ARRAY(SELECT o.slug FROM mentor_service_offerings m "
            "             JOIN service_offerings o ON o.id = m.service_offering_id "
            "             WHERE m.mentor_user_id = u.id ORDER BY o.sort_order) AS offerings "
            # Mentors only: the demo mentees share the email suffix, and a session
            # type written for one fails the key to `mentor_profiles`.
            "FROM users u JOIN mentor_profiles mp ON mp.user_id = u.id "
            "WHERE u.email LIKE :s ORDER BY u.email"
        ),
        {"s": f"%@{DEMO_DOMAIN}"},
    )
    result = []
    for row in rows:
        offerings = list(row.offerings or ())
        if row.first in offerings:
            offerings.remove(row.first)
            offerings.insert(0, row.first)
        result.append((row.id, tuple(offerings)))
    return result


async def _history(
    session: AsyncSession,
    mentor_id: Any,
    session_type: Any,
    mentor: DemoMentor,
    *,
    now: dt.datetime,
) -> None:
    """Completed sessions, and reviews from the demo mentees who had them.

    One demo mentee per review (a mentee reviews a mentor once), and at least
    one mentee whenever there are sessions to have had.
    """
    mentee_count = max(len(mentor.ratings), 1 if mentor.completed_sessions else 0)
    mentees = [
        await _one(
            session,
            "INSERT INTO users (email, first_name, primary_role, timezone) "
            "VALUES (:e, 'Demo', 'mentee', 'UTC') RETURNING id",
            {"e": _email(f"{mentor.key}-mentee-{n}")},
        )
        for n in range(mentee_count)
    ]
    for n in range(mentor.completed_sessions):
        await session.execute(
            text(
                "INSERT INTO sessions "
                "(mentor_id, mentee_id, session_type_id, starts_at, duration_minutes, status) "
                "VALUES (:m, :e, :t, :s, 45, 'completed')"
            ),
            {
                "m": mentor_id,
                "e": mentees[n % len(mentees)],
                "t": session_type,
                "s": now - dt.timedelta(days=3 + 2 * n),
            },
        )
    for mentee, rating in zip(mentees, mentor.ratings, strict=False):
        await session.execute(
            text(
                "INSERT INTO reviews (reviewed_by, reviewed_for, communication_rating, "
                "knowledge_rating, practicality_rating, support_rating, valuable_rating, "
                "nps_recommend_score, public_review) "
                "VALUES (:by, :for_, :o, :o, :o, :o, :r, 9, :t)"
            ),
            {
                "by": mentee,
                "for_": mentor_id,
                "r": rating,
                # The four detailed ratings are on the 1..3 scale; only "how
                # valuable" is 1..5. A strong review is strong on both.
                "o": 3 if rating >= 4 else 2 if rating == 3 else 1,
                "t": f"{mentor.first_name} helped me plan my next step.",
            },
        )


async def catalogue_offerings(session: AsyncSession) -> list[str]:
    """The live service-offering slugs, in display order — what demo mentors pick from."""
    rows = await session.execute(
        text("SELECT slug FROM service_offerings WHERE is_active ORDER BY sort_order")
    )
    return [str(slug) for slug in rows.scalars()]


async def demo_avatar_urls(session: AsyncSession) -> list[str]:
    """Stored avatar URLs of every demo user, for the storage objects to go too."""
    rows = await session.execute(
        text(
            "SELECT p.avatar_url FROM user_profiles p JOIN users u ON u.id = p.user_id "
            "WHERE u.email LIKE :suffix AND p.avatar_url IS NOT NULL"
        ),
        {"suffix": f"%@{DEMO_DOMAIN}"},
    )
    return [str(url) for url in rows.scalars()]


#: Written out in full rather than composed, so no statement is built from
#: strings: the one value, the suffix, is bound. Order matters — each clears
#: what a later delete's `RESTRICT` key would refuse.
_REMOVAL = (
    text(
        "DELETE FROM reviews WHERE reviewed_by IN (SELECT id FROM users WHERE email LIKE :suffix) "
        "OR reviewed_for IN (SELECT id FROM users WHERE email LIKE :suffix)"
    ),
    text(
        "DELETE FROM session_events WHERE session_id IN (SELECT s.id FROM sessions s "
        "JOIN users u ON u.id IN (s.mentor_id, s.mentee_id) WHERE u.email LIKE :suffix)"
    ),
    text(
        "DELETE FROM session_participants WHERE session_id IN (SELECT s.id FROM sessions s "
        "JOIN users u ON u.id IN (s.mentor_id, s.mentee_id) WHERE u.email LIKE :suffix)"
    ),
    text(
        "DELETE FROM sessions WHERE id IN (SELECT s.id FROM sessions s "
        "JOIN users u ON u.id IN (s.mentor_id, s.mentee_id) WHERE u.email LIKE :suffix)"
    ),
    # An offering's key restricts its mentor's profile, so offerings go first.
    text(
        "DELETE FROM session_type_booking_configs WHERE session_type_id IN (SELECT t.id "
        "FROM session_types t JOIN users u ON u.id = t.mentor_user_id "
        "WHERE u.email LIKE :suffix)"
    ),
    text(
        "DELETE FROM session_type_scheduling_windows WHERE session_type_id IN (SELECT t.id "
        "FROM session_types t JOIN users u ON u.id = t.mentor_user_id "
        "WHERE u.email LIKE :suffix)"
    ),
    text(
        "DELETE FROM session_types WHERE mentor_user_id IN "
        "(SELECT id FROM users WHERE email LIKE :suffix)"
    ),
)


class RealHistoryError(Exception):
    """A real user has history with a demo user, so nothing was removed.

    A tester booking or reviewing a demo mentor on dev is the point of having
    them. Deleting that session would erase a real person's history — and a
    credit movement points at it with a `RESTRICT` key, so the delete would
    fail half-way anyway. A person decides what happens to it.
    """


#: Sessions and reviews joining a demo user to someone who is not one.
_REAL_HISTORY = text(
    "SELECT (SELECT count(*) FROM sessions s "
    "  JOIN users m ON m.id = s.mentor_id JOIN users e ON e.id = s.mentee_id "
    "  WHERE (m.email LIKE :suffix) <> (e.email LIKE :suffix)) "
    "+ (SELECT count(*) FROM reviews r "
    "  JOIN users b ON b.id = r.reviewed_by JOIN users f ON f.id = r.reviewed_for "
    "  WHERE (b.email LIKE :suffix) <> (f.email LIKE :suffix))"
)


async def remove_demo(session: AsyncSession) -> int:
    """Delete every demo user and everything they own. Returns how many users.

    **Refuses, deleting nothing, if a real user has a session with or a review
    of a demo user** (`RealHistoryError`). Otherwise every session and review
    touching a demo user involves only demo users, and goes first — their keys
    to users `RESTRICT`. Does not commit.
    """
    demo = {"suffix": f"%@{DEMO_DOMAIN}"}
    if (shared := int((await session.execute(_REAL_HISTORY, demo)).scalar_one())) > 0:
        raise RealHistoryError(
            f"{shared} session(s) or review(s) link a real user to a demo user; nothing was removed"
        )
    for statement in _REMOVAL:
        await session.execute(statement, demo)
    removed = await session.execute(text("DELETE FROM users WHERE email LIKE :suffix"), demo)
    return int(removed.rowcount or 0)  # type: ignore[attr-defined]
