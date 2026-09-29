"""Seed a dev environment with demo mentors, so the explore page is full.

    railway run uv run python scripts/seed_demo_mentors.py --avatars <folder>
    railway run uv run python scripts/seed_demo_mentors.py --remove
    railway run uv run python scripts/seed_demo_mentors.py --session-types

**Never production.** It refuses before touching anything when the environment
is `production`, whatever else is configured.

`--avatars` is a folder holding `manifest.json` (`people`: `file`,
`first_name`, `last_name`) and the images it names. The images stay outside the
repository, which is public: they belong to their licensors, and seeding dev
does not need them in git. Each goes through the real avatar path — validated,
resized and re-encoded, then stored — exactly as a mentor's own upload would.

Re-running replaces the demo set: everything demo is removed first, including
its stored avatars. Every demo user's email ends in `@demo.edufurther.test`, and
`--remove` deletes exactly those. Two further mentors have no photo, for the
initials path.

`--session-types` upgrades the demo mentors already seeded, in place: each gets
the catalogue's session types for their offerings plus an intro call — 2 to 4,
each with a description, duration, notice, stage and intake questions. Their
original single type becomes the first one, so their history stays attached.
Safe to run again.

The roster below is data; the SQL is in `app.infra.db.demo_seed` (a script may
hold none).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_storage
from app.core.config import Settings, get_settings
from app.domain.assets import AssetKind
from app.infra.db.asset_store import store_image
from app.infra.db.demo_seed import (
    DemoMentor,
    DemoQuestion,
    DemoSessionType,
    RealHistoryError,
    apply_demo_session_types,
    catalogue_offerings,
    create_demo_mentor,
    demo_avatar_urls,
    demo_mentor_offerings,
    remove_demo,
)
from app.infra.db.engine import create_database_engine
from app.infra.etl.cli import EXIT_OK, EXIT_REFUSED, configure_streams

DAY = 1440
Q = DemoQuestion

#: What a demo mentor can be booked for, by the service offering it serves.
#: The first entry is a mentor's main type for that offering; a second is
#: added only while the mentor has room (at most four types in all).
CATALOGUE: dict[str, tuple[DemoSessionType, ...]] = {
    "test-preparation": (
        DemoSessionType(
            name="IELTS & TOEFL game plan",
            description=(
                "We look at where your scores are, where they need to be, and build a "
                "study plan for the weeks you have left. Bring a recent practice score."
            ),
            offering="test-preparation",
            duration=45,
            notice=DAY,
            stage="early_exploration",
            questions=(
                Q("Which test are you taking, and when?", required=True),
                Q("What is your latest practice score, and your target?"),
            ),
        ),
    ),
    "document-preparation": (
        DemoSessionType(
            name="Personal statement review",
            description=(
                "A line-by-line read of your statement: structure, story and what the "
                "committee will actually remember. Share your draft before we meet."
            ),
            offering="document-preparation",
            duration=60,
            notice=2 * DAY,
            stage="drafting_stage",
            questions=(
                Q("Which programme is this statement for?", required=True),
                Q("Paste a link to your current draft (a shared Google Doc is fine)."),
            ),
        ),
        DemoSessionType(
            name="CV and résumé polish",
            description=(
                "Thirty focused minutes on the one page that gets you read: what to cut, "
                "what to lead with, and how to show impact."
            ),
            offering="document-preparation",
            duration=30,
            notice=DAY,
            stage="revisions",
            questions=(Q("What role or programme is this CV for?", required=True),),
        ),
    ),
    "school-selection": (
        DemoSessionType(
            name="Build your university shortlist",
            description=(
                "From a long list to five schools you can defend: fit, funding, "
                "admission odds and life after the degree."
            ),
            offering="school-selection",
            duration=45,
            notice=DAY,
            stage="early_exploration",
            questions=(
                Q("Which countries are you considering?", required=True),
                Q("Roughly what can you spend per year, including living costs?"),
            ),
        ),
    ),
    "program-selection": (
        DemoSessionType(
            name="Choosing the right programme",
            description=(
                "Taught or research, one year or two, conversion or specialist — we "
                "match programmes to where you want to be in five years."
            ),
            offering="program-selection",
            duration=45,
            notice=DAY,
            stage="early_exploration",
            questions=(
                Q("What did you study, and what do you want to study next?", required=True),
            ),
        ),
    ),
    "scholarships-financial-aid": (
        DemoSessionType(
            name="Scholarship strategy",
            description=(
                "Which scholarships you are competitive for, what each committee looks "
                "for, and a timeline that gets every application in on time."
            ),
            offering="scholarships-financial-aid",
            duration=60,
            notice=2 * DAY,
            stage="early_exploration",
            questions=(
                Q("Which scholarships are you considering?", required=True),
                Q("When is your earliest deadline?"),
            ),
        ),
    ),
    "interview-preparation": (
        DemoSessionType(
            name="Mock interview with feedback",
            description=(
                "A full practice interview in the format you will face, then honest "
                "feedback on every answer and what to change before the real one."
            ),
            offering="interview-preparation",
            duration=60,
            notice=2 * DAY,
            stage="post_submission",
            questions=(
                Q("Which programme or scholarship is the interview for?", required=True),
                Q("When is your interview?"),
            ),
        ),
    ),
}

#: Every demo mentor also offers a short first conversation.
INTRO = DemoSessionType(
    name="Intro call",
    description="Twenty minutes to talk about your plans and see whether we are a good fit.",
    offering=None,
    duration=20,
    notice=DAY,
    stage=None,
    questions=(Q("What would you like to get out of this call?", required=True),),
)

#: A mentor's types in all: their offerings' types, then the intro call.
MAX_TYPES = 4


def session_types_for(offerings: tuple[str, ...]) -> tuple[DemoSessionType, ...]:
    """Each offering's main type, a second one while there is room, then the intro."""
    main = [CATALOGUE[slug][0] for slug in offerings if slug in CATALOGUE]
    extra = [t for slug in offerings if slug in CATALOGUE for t in CATALOGUE[slug][1:]]
    room = MAX_TYPES - 1 - len(main)
    return (*main[: MAX_TYPES - 1], *extra[: max(room, 0)], INTRO)


#: Varied on purpose, so filters, the academic line and search all have work.
SCHOOLS = (
    ("University of Oxford", "GB", "masters", "Public Policy"),
    ("Imperial College London", "GB", "masters", "Computer Science"),
    ("Harvard University", "US", "masters", "Public Health"),
    ("University of Toronto", "CA", "doctorate", "Economics"),
    ("ETH Zurich", "CH", "masters", "Mechanical Engineering"),
    ("University of Cape Town", "ZA", "bachelors", "Accounting"),
    ("University of Lagos", "NG", "bachelors", "Law"),
    ("TU Munich", "DE", "masters", "Data Science"),
    ("University of Melbourne", "AU", "doctorate", "Biomedical Science"),
    ("McGill University", "CA", "bachelors", "Architecture"),
    ("KU Leuven", "BE", "masters", "Development Studies"),
    ("Stanford University", "US", "doctorate", "Mathematics"),
)
ORIGINS = ("NG", "GH", "KE", "ZA", "EG", "IN", "BR", "PH", "PL", "IT", "US", "GB")
ZONES = ("Africa/Lagos", "Europe/London", "America/Toronto", "Africa/Nairobi", "Europe/Berlin")
HEADLINES = (
    "Scholarship winner, happy to review your essays",
    "Admissions interviewer for three years",
    "Helped 40+ students get funded offers",
    "Career switcher into tech, now mentoring",
    "PhD candidate, research proposals and CVs",
    "First-generation graduate, applications coach",
)
#: 0 to 60 completed sessions, with a few new mentors at zero.
SESSIONS = (0, 2, 5, 60, 12, 1, 30, 0, 8, 45, 3, 20, 0, 15, 52, 6, 25, 2, 40, 10, 0, 18, 7, 35)
#: Some unreviewed, some glowing, a few mixed.
RATINGS: tuple[tuple[int, ...], ...] = (
    (),
    (5,),
    (5, 4),
    (5, 5, 5, 4, 5, 5, 5, 5, 5, 5, 4, 5),
    (4, 3),
    (),
    (5, 5, 4, 5),
    (),
    (3,),
    (5, 5, 5, 5, 5, 4, 5, 5, 5, 5),
    (4,),
    (5, 4, 5),
    (),
    (5, 5),
    (5, 5, 5, 5, 4),
    (2, 4),
    (5, 4, 4),
    (),
    (5, 5, 5),
    (4, 5),
    (),
    (3, 4),
    (5,),
    (5, 5, 4, 4),
)
#: Two mentors are fully booked across the horizon, so their cards read `none`.
BLOCKED = {5, 17}
#: For the initials path: no photo.
NO_PHOTO = (("Amara", "Okafor"), ("Chen", "Wei"))


def roster(
    people: list[dict[str, str]], offerings: list[str]
) -> list[tuple[DemoMentor, str | None]]:
    """Each demo mentor, paired with the face file it uses (or `None`)."""
    faces: list[tuple[str, str, str | None]] = [
        (p["first_name"], p["last_name"], p["file"]) for p in people
    ]
    faces += [(first, last, None) for first, last in NO_PHOTO]
    mentors = []
    for n, (first, last, face) in enumerate(faces):
        school, study, level, course = SCHOOLS[n % len(SCHOOLS)]
        picked = tuple(offerings[(n + k) % len(offerings)] for k in range(1 + n % 3))
        mentor = DemoMentor(
            key=f"demo-{n:02d}-{first.lower()}",
            first_name=first,
            last_name=last,
            headline=HEADLINES[n % len(HEADLINES)],
            about_me=(
                f"I studied {course} at {school}. I mentor applicants through "
                "the process I went through myself, from the first draft to the offer."
            ),
            timezone=ZONES[n % len(ZONES)],
            study_country=study,
            origin_country=ORIGINS[n % len(ORIGINS)],
            degree_level=level,
            course=course,
            school=school,
            offerings=picked,
            session_types=session_types_for(picked),
            completed_sessions=SESSIONS[n % len(SESSIONS)],
            ratings=RATINGS[n % len(RATINGS)],
            open=n not in BLOCKED,
        )
        mentors.append((mentor, face))
    return mentors


def refuse(settings: Settings) -> str | None:
    """Why this run must not proceed, or `None` when it may.

    **The environment must be set explicitly**, not merely not be production.
    It defaults to `local`, so a run against a production database from a
    shell that never set it would pass a plain "is it production?" check.
    `model_fields_set` holds the settings that were actually provided.
    """
    if "environment" not in settings.model_fields_set:
        return "EDUFURTHER_ENVIRONMENT is not set; refusing to guess where this is"
    if settings.environment == "production":
        return "refusing to seed demo mentors into production"
    return None


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if (why := refuse(settings)) is not None:
        print(why)
        return EXIT_REFUSED

    engine = create_database_engine(settings)
    try:
        if args.session_types:
            return await _upgrade_session_types(engine)
        async with AsyncSession(engine) as session:
            old_avatars = await demo_avatar_urls(session)
            try:
                removed = await remove_demo(session)
            except RealHistoryError as exc:
                print(f"{exc}. Resolve those first; the demo set is unchanged.")
                return EXIT_REFUSED
            await session.commit()
            print(f"removed {removed} demo users")
            # **After the commit**: deleting the images first would leave the
            # profiles pointing at nothing if the removal then failed.
            if old_avatars:
                await _delete_avatars(get_storage(), old_avatars)
            if args.remove:
                return EXIT_OK
            storage = get_storage()

            folder = Path(args.avatars)
            people = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))["people"]
            now = dt.datetime.now(dt.UTC)
            offerings = await catalogue_offerings(session)
            for mentor, face in roster(people, offerings):
                user_id: UUID = await create_demo_mentor(session, mentor, now=now)
                await session.commit()
                if face is not None:
                    await _avatar(session, storage, user_id, folder / face)
                print(f"seeded {mentor.first_name} {mentor.last_name}")
            # No next-free-time refresh here: it would recompute real mentors
            # too, without their calendars. The scheduled job fills these cards
            # within one run.
    finally:
        await engine.dispose()
    return EXIT_OK


async def _upgrade_session_types(engine: object) -> int:
    """Give every demo mentor already seeded the catalogue's session types."""
    async with AsyncSession(engine) as session:  # type: ignore[arg-type]
        mentors = await demo_mentor_offerings(session)
        if not mentors:
            print("no demo mentors to upgrade")
            return EXIT_OK
        for user_id, offerings in mentors:
            types = session_types_for(offerings)
            await apply_demo_session_types(session, user_id, types)
            await session.commit()
            print(f"{user_id}: {', '.join(t.name for t in types)}")
        print(f"upgraded {len(mentors)} demo mentors")
    return EXIT_OK


async def _delete_avatars(storage: object, urls: list[str]) -> None:
    for url in urls:
        await asyncio.to_thread(storage.drop_url, url)  # type: ignore[attr-defined]


async def _avatar(session: AsyncSession, storage: object, user_id: UUID, file: Path) -> None:
    """The real avatar path — `store_image`, the upload endpoint's own pipeline."""
    await store_image(session, storage, AssetKind.AVATAR, user_id, file.read_bytes())  # type: ignore[arg-type]
    await session.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--avatars", help="Folder holding manifest.json and the face images.")
    group.add_argument("--remove", action="store_true", help="Remove every demo mentor.")
    group.add_argument(
        "--session-types",
        action="store_true",
        help="Give the demo mentors already seeded the catalogue's session types, in place.",
    )
    configure_streams()
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
