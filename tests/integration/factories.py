"""Row factories shared by the public-endpoint tests.

**Not a `conftest.py`, deliberately.** It defines no fixtures and no hooks —
only plain helpers — and eighteen test modules import shared helpers by the
bare name `from conftest import ...`, which resolves to whichever `conftest`
reaches `sys.path` first. A nested one shadows `tests/conftest.py` and breaks
every one of them at collection. A module of helpers is a module.

**Here rather than copied into each file.** `make_user` is currently defined in
seven integration modules, each drifted toward whatever its own tests needed —
recorded in `failure-modes.md` as debt this project should stop adding to. A
public mentor is now needed by two suites, so it goes in one place before there
is a second copy rather than after there are nine.

The scope is deliberately narrow: a mentor who may be seen publicly, and one
thing they offer. Availability windows stay with the slots tests, because only
those care.
"""

from __future__ import annotations

import datetime as dt
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

LAGOS = "Africa/Lagos"


async def make_public_mentor(
    engine: AsyncEngine,
    tag: str,
    *,
    approved: bool = True,
    listed: bool = True,
    timezone: str = LAGOS,
    #: The legacy public profile handle. Nullable in the schema and on 4 of 43
    #: migrated users, so a mentor without one must stay reachable by id.
    slug: str | None = None,
) -> UUID:
    """A mentor and their profile, with every reason to be refused as a knob.

    Returns the mentor's user id. No session type — `add_session_type` does
    that, so a test can give one mentor several or none.
    """
    async with engine.begin() as conn:
        mentor = (
            await conn.execute(
                text(
                    "INSERT INTO users "
                    "(email, auth_id, first_name, last_name, slug, primary_role, timezone) "
                    "VALUES (:e, :a, 'Ada', 'Lovelace', :s, 'mentor', :z) RETURNING id"
                ),
                {"e": f"mentor-{tag}@example.test", "a": uuid4(), "s": slug, "z": timezone},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO mentor_profiles "
                "(user_id, headline, approval_status, listing_status) "
                "VALUES (:u, 'M', CAST(:a AS approval_status), CAST(:l AS listing_status))"
            ),
            {
                "u": mentor,
                "a": "approved" if approved else "pending",
                "l": "listed" if listed else "unlisted",
            },
        )
    return mentor


async def add_session_type(
    engine: AsyncEngine,
    mentor: UUID,
    *,
    name: str = "General Mentorship",
    description: str | None = None,
    category: str | None = None,
    application_stage: str | None = None,
    duration: int = 45,
    notice: int = 0,
    venue: str | None = None,
    active: bool = True,
    deleted: bool = False,
    config: bool = True,
) -> UUID:
    """One offering for a mentor, with each way of being invisible as a knob."""
    async with engine.begin() as conn:
        session_type = (
            await conn.execute(
                text(
                    "INSERT INTO session_types "
                    "(mentor_user_id, name, description, category, application_stage, is_active) "
                    "VALUES (:u, :n, :d, :c, :s, :active) RETURNING id"
                ),
                {
                    "u": mentor,
                    "n": name,
                    "d": description,
                    "c": category,
                    "s": application_stage,
                    "active": active,
                },
            )
        ).scalar_one()
        if config:
            # `meeting_venue` omitted rather than passed as NULL when the caller
            # said nothing. It is NOT NULL with a server default since D88's
            # reader step, and an explicit NULL overrides a server default — so
            # naming the column unconditionally would fail every offering that
            # does not care which venue it is on.
            columns = "(session_type_id, duration_minutes, min_notice_minutes"
            values = "(:t, :d, :n"
            params: dict[str, object] = {"t": session_type, "d": duration, "n": notice}
            if venue is not None:
                columns += ", meeting_venue"
                values += ", CAST(:v AS meeting_provider)"
                params["v"] = venue
            await conn.execute(
                text(f"INSERT INTO session_type_booking_configs {columns}) VALUES {values})"),  # noqa: S608
                params,
            )
        if deleted:
            # After the config exists, which is the order a real deletion takes:
            # the config row stays and the join still finds it, so only the
            # predicate keeps this offering out.
            await conn.execute(
                text("UPDATE session_types SET deleted_at = now() WHERE id = :t"),
                {"t": session_type},
            )
    return session_type


async def add_availability(
    engine: AsyncEngine,
    mentor: UUID,
    *,
    day_of_week: int = 1,
    start: str = "09:00",
    end: str = "12:00",
    timezone: str = LAGOS,
    active: bool = True,
    deleted: bool = False,
) -> None:
    """One weekly window, with each way of not counting as a knob.

    Shared because discovery made this the fourth place the same insert was
    written. The other three are inline in the slots and session-type suites and
    predate this file; they are candidates for the same treatment whenever
    somebody is in there anyway.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO availability_rules "
                "(mentor_user_id, day_of_week, start_time, end_time, timezone, "
                " is_active, deleted_at) "
                "VALUES (:u, :d, :s, :e, :z, :a, CASE WHEN :x THEN now() END)"
            ),
            {
                "u": mentor,
                "d": day_of_week,
                # asyncpg binds a `time` column from a `time` object, not a
                # string — the other suites inline the literal in SQL and so
                # never meet this.
                "s": dt.time.fromisoformat(start),
                "e": dt.time.fromisoformat(end),
                "z": timezone,
                "a": active,
                "x": deleted,
            },
        )


async def make_bookable_mentor(engine: AsyncEngine, tag: str, **knobs: object) -> UUID:
    """A mentor who actually appears in discovery: public, offering, and free hours.

    Three things have to be true at once, and each is a separate reason to be
    missing — so the refusal tests build this and remove exactly one.
    """
    mentor = await make_public_mentor(engine, tag, **knobs)  # type: ignore[arg-type]
    await add_session_type(engine, mentor)
    await add_availability(engine, mentor)
    return mentor


async def add_education(
    engine: AsyncEngine,
    user: UUID,
    *,
    #: `None` leaves `degree_level_id` null, which the column permits and the
    #: API write path allows — a user may record a school without saying what
    #: level it was.
    level: str | None = "doctorate",
    #: What the card shows and what a mentee thinks of as the subject.
    course: str | None = "Mathematics",
    #: A *different* column holding degree names — `BSc (Bachelor of Science)`.
    #: Both are searchable and only `course` is displayed; naming them apart
    #: here is the point, because one helper that wrote whichever of the two
    #: its author had in mind is how they were confused in the first place.
    program: str | None = None,
    school: str = "Washington University",
    abbreviation: str | None = None,
    institution: bool = True,
    date_end: str = "2026-01-01",
    deleted: bool = False,
) -> UUID:
    """One education entry, with every axis the card reads as a knob.

    `institution=False` leaves `institution_id` null so the card must fall back
    to `school_name_raw` — the pair ADR 0008 point 5 exists for, where an
    unmatched school still displays what the user typed.
    """
    async with engine.begin() as conn:
        institution_id = None
        if institution:
            institution_id = (
                await conn.execute(
                    text(
                        "INSERT INTO institutions (name, domain, source) "
                        "VALUES (:n, :d, 'hipolabs') RETURNING id"
                    ),
                    {"n": school, "d": f"{uuid4().hex[:8]}.example"},
                )
            ).scalar_one()
        return (
            await conn.execute(
                text(
                    "INSERT INTO education_entries "
                    "(user_id, school_name_raw, institution_id, degree_level_id, "
                    " degree_abbreviation, study_course, study_program, date_end, deleted_at) "
                    "SELECT :u, :raw, :inst, "
                    "       (SELECT id FROM degree_levels WHERE slug = :level), "
                    "       :abbrev, :course, :program, :ends, "
                    "       CASE WHEN :deleted THEN now() END RETURNING id"
                ),
                {
                    "u": user,
                    "raw": school,
                    "inst": institution_id,
                    "abbrev": abbreviation,
                    "course": course,
                    "program": program,
                    "ends": dt.date.fromisoformat(date_end),
                    "level": level,
                    "deleted": deleted,
                },
            )
        ).scalar_one()


async def add_completed_sessions(
    engine: AsyncEngine, mentor: UUID, count: int, *, status: str = "completed"
) -> None:
    """Sessions in a terminal state, spaced so the overlap constraint stays happy.

    `sessions_no_mentor_double_booking` only covers live statuses, so completed
    rows could overlap — they are spaced anyway, because a fixture that relies on
    a constraint not applying breaks the day the constraint widens.
    """
    async with engine.begin() as conn:
        mentee = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, first_name, primary_role, timezone) "
                    "VALUES (:e, 'Mentee', 'mentee', 'Africa/Lagos') RETURNING id"
                ),
                {"e": f"mentee-{uuid4()}@example.test"},
            )
        ).scalar_one()
        session_type = (
            await conn.execute(
                text("SELECT id FROM session_types WHERE mentor_user_id = :m LIMIT 1"),
                {"m": mentor},
            )
        ).scalar_one_or_none()
        for index in range(count):
            await conn.execute(
                text(
                    "INSERT INTO sessions "
                    "(mentor_id, mentee_id, session_type_id, starts_at, duration_minutes, status) "
                    "VALUES (:m, :e, :t, :starts, 45, "
                    "        CAST(:status AS session_status))"
                ),
                {
                    "m": mentor,
                    "e": mentee,
                    "t": session_type,
                    "starts": dt.datetime.now(dt.UTC) - dt.timedelta(days=index + 1),
                    "status": status,
                },
            )
