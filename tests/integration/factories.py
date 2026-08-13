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
    #: `mentor_profiles.default_meeting_venue` is NOT NULL with a server
    #: default, so "no default" is not a state a mentor can be in.
    default_venue: str = "google_meet",
    custom_meeting_url: str | None = None,
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
                "(user_id, headline, approval_status, listing_status, "
                " default_meeting_venue, custom_meeting_url) "
                "VALUES (:u, 'M', CAST(:a AS approval_status), CAST(:l AS listing_status), "
                "        CAST(:v AS meeting_provider), :c)"
            ),
            {
                "u": mentor,
                "a": "approved" if approved else "pending",
                "l": "listed" if listed else "unlisted",
                "v": default_venue,
                "c": custom_meeting_url,
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
            await conn.execute(
                text(
                    "INSERT INTO session_type_booking_configs "
                    "(session_type_id, duration_minutes, min_notice_minutes, meeting_venue) "
                    "VALUES (:t, :d, :n, CAST(:v AS meeting_provider))"
                ),
                {"t": session_type, "d": duration, "n": notice, "v": venue},
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
