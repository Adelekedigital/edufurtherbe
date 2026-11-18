"""The rehearsal reset against a migrated database: what it clears, what it keeps,
and when it refuses.

Seeded through the same factories and writers the app uses, then reset. Each
test owns its database (``isolated_db``): the reset opens its own transaction,
``READ ONLY`` for a dry run, which a savepoint cannot host.
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession
from tests.integration.factories import add_session, make_bookable_mentor, make_institution

from app.domain import app_reset
from app.domain.app_reset import DEFAULT_REAL_DATA_FROM, Target
from app.domain.enums import LinkVia, ShareMethod
from app.domain.provisioning import Action, Candidate, plan_for
from app.infra.db.calendar_store import NullFreeBusy
from app.infra.db.demo_seed import DemoMentor, create_demo_mentor
from app.infra.db.link_tracking import record_share, record_visit
from app.infra.db.next_available_store import refresh_next_available
from app.infra.db.provisioning_store import ProvisioningStore
from app.infra.etl import reset as reset_module
from app.infra.etl.cli import EXIT_OK, EXIT_REFUSED, EXIT_UNRESOLVED
from app.infra.etl.reset import (
    CATALOGUE,
    CLEAR,
    KEEP,
    NOT_ACTIVITY,
    ResetVerificationError,
    reset,
    run,
)
from app.infra.storage.supabase import StorageError
from conftest import PLATFORM_WINDOW, add_user

pytestmark = [pytest.mark.db, pytest.mark.asyncio, pytest.mark.isolated_db]

NOW = dt.datetime.now(dt.UTC)
#: Everything seeded now is rehearsal data under this cutoff.
LATER = NOW + dt.timedelta(days=1)
#: Everything seeded now is real data under this one.
EARLIER = NOW - dt.timedelta(hours=1)
#: The latest cutoff these tests allow. Rows are seeded now and must fall
#: before `LATER`, so the cap moves with the clock once the real one (a fixed
#: date) has passed; `test_app_reset_rules` pins the real value.
CAP = max(DEFAULT_REAL_DATA_FROM, LATER)


@pytest.fixture(autouse=True)
def _cap_after_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_reset, "DEFAULT_REAL_DATA_FROM", CAP)


LINK_TABLES = ("mentor_profile_visits", "mentor_profile_shares", "visitor_day_keys")

MENTOR = DemoMentor(
    key="reset-open",
    first_name="Sofia",
    last_name="Marin",
    headline="Chevening scholar",
    about_me="I help applicants tell their story.",
    timezone="Africa/Lagos",
    study_country="GB",
    origin_country="NG",
    degree_level="masters",
    course="Public Policy",
    school="University of Oxford",
    offerings=("test-preparation",),
    completed_sessions=3,
    ratings=(5, 4),
)


async def _tables(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        return {row[0] for row in rows}


async def _counts(engine: AsyncEngine, tables: set[str]) -> dict[str, int]:
    async with engine.connect() as conn:
        return {
            table: int(
                (await conn.execute(text(f'SELECT count(*) FROM "{table}"'))).scalar_one()  # noqa: S608
            )
            for table in sorted(tables)
        }


async def _checksums(engine: AsyncEngine) -> dict[str, tuple[int, str]]:
    """Every kept row, hashed independently of the module's own fingerprint."""
    result = {}
    async with engine.connect() as conn:
        for table in sorted(KEEP | set(CATALOGUE)):
            where = f"WHERE {CATALOGUE[table]} IS NULL" if table in CATALOGUE else ""
            row = (
                await conn.execute(
                    text(
                        f"SELECT count(*), md5(coalesce(string_agg(to_jsonb(t)::text, '|' "  # noqa: S608
                        f"ORDER BY to_jsonb(t)::text), '')) FROM \"{table}\" t {where}"
                    )
                )
            ).one()
            result[table] = (int(row[0]), str(row[1]))
    return result


async def _migrated(engine: AsyncEngine) -> None:
    """Every account as the legacy load leaves it."""
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE users SET signup_source = 'migrated'"))


#: What a dress run's people do in the app beyond the factories: messages,
#: credits, invites, push, consent. Rows that ``RESTRICT`` their parents, so a
#: wrong deletion order fails here.
_ACTIVITY = (
    "INSERT INTO conversations (mentee_user_id, mentor_user_id, status) "
    "VALUES (:mentee, :mentor, 'accepted')",
    "INSERT INTO conversation_participants (conversation_id, user_id) "
    "SELECT id, :mentee FROM conversations",
    "INSERT INTO messages (conversation_id, sender_id, message_type, body) "
    "SELECT id, :mentee, 'text', 'hello' FROM conversations",
    "INSERT INTO credit_lots (user_id, source, quantity_granted, quantity_remaining) "
    "VALUES (:mentee, 'monthly_free', 3, 2)",
    "INSERT INTO credit_transactions (user_id, credit_lot_id, delta, reason, session_id) "
    "SELECT :mentee, id, -1, 'session_booked', :session FROM credit_lots",
    "INSERT INTO invite_links (user_id, code) VALUES (:mentee, 'ada-x1y2')",
    "INSERT INTO referrals (referrer_id, code, invitee_user_id) VALUES (:mentee, 'r-1', :mentor)",
    "INSERT INTO user_activity_days (user_id, day) VALUES (:mentee, CURRENT_DATE)",
    "INSERT INTO outbox_events (entity_id, entity_type, event_type, destination) "
    "VALUES (:session, 'session', 'session_booked', 'email')",
    "INSERT INTO idempotency_keys (user_id, key, request_hash, endpoint) "
    "VALUES (:mentee, 'k1', 'h', 'POST /sessions')",
    "INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth) "
    "VALUES (:mentee, 'https://fcm.googleapis.com/x', 'p', 'a')",
    "INSERT INTO analytics_consents (user_id, version, granted) VALUES (:mentee, 'v1', true)",
    "INSERT INTO feature_interest (user_id, feature) VALUES (:mentee, 'payments')",
    "INSERT INTO writing_assist_requests (user_id, field) VALUES (:mentee, 'mentee_bio')",
    "INSERT INTO notification_channel_preferences (user_id, notification_type) "
    "VALUES (:mentee, 'unread_messages')",
    "INSERT INTO user_links (user_id, platform, url) VALUES (:mentee, 'x', 'https://x.com/ada')",
    "INSERT INTO intake_files (uploader_id, session_id, storage_key, filename, size_bytes, "
    "content_type) VALUES (:mentee, :session, CAST(:mentee AS uuid)::text || '/cv', "
    "'cv.pdf', 10, 'application/pdf')",
    "INSERT INTO intake_files (uploader_id, storage_key, filename, size_bytes, content_type, "
    "deleted_at, purged_at) VALUES (:mentee, CAST(:mentee AS uuid)::text || '/gone', "
    "'old.pdf', 10, 'application/pdf', now(), now())",
    "INSERT INTO message_attachments (uploaded_by, storage_key, file_name, mime_type, "
    "size_bytes) VALUES (:mentee, 'messages/' || CAST(:mentee AS uuid)::text || '/draft', 'd.pdf', "
    "'application/pdf', 10)",
)

#: The objects the seed's rows name (the purged one's object is already gone).
SEEDED_FILES = 2


def _gone(_key: str) -> bool:
    """A bucket where every removal succeeds."""
    return True


async def _app_activity(
    conn: AsyncConnection, *, mentor: UUID, mentee: UUID, session: UUID
) -> None:
    for statement in _ACTIVITY:
        await conn.execute(
            text(statement), {"mentor": mentor, "mentee": mentee, "session": session}
        )


async def seed_rehearsal(engine: AsyncEngine) -> UUID:
    """A dress run: mentors with history, sessions, link tracking and a typed school."""
    async with AsyncSession(engine) as session:
        demo = await create_demo_mentor(session, MENTOR, now=NOW)
        await session.commit()
        await refresh_next_available(
            session, max_age=dt.timedelta(minutes=5), reader=NullFreeBusy(), window=PLATFORM_WINDOW
        )
        for method in ShareMethod:
            await record_share(
                session,
                mentor_id=demo,
                day=NOW.date(),
                visitor_key=os.urandom(32),
                signed_in=True,
                method=method,
                own=False,
            )
        await record_visit(
            session,
            mentor_id=demo,
            day=NOW.date(),
            visitor_key=os.urandom(32),
            signed_in=False,
            via=LinkVia.COPY,
            referrer_host="example.com",
        )
        await session.execute(
            text("INSERT INTO visitor_day_keys (day, key) VALUES (:d, :k)"),
            {"d": NOW.date(), "k": os.urandom(32)},
        )
        await session.commit()

    mentor = await make_bookable_mentor(engine, "reset")
    async with engine.begin() as conn:
        mentee = await add_user(conn, f"mentee-{uuid4()}@example.test")
    booked = await add_session(engine, mentor, mentee=mentee)
    await make_institution(engine, "Registry University")
    async with engine.begin() as conn:
        await _app_activity(conn, mentor=mentor, mentee=mentee, session=booked)
        typist = await add_user(conn, f"typist-{uuid4()}@example.test", auth_id=uuid4())
        await conn.execute(
            text(
                "INSERT INTO institutions (name, source, status, created_by) "
                "VALUES ('Typed College', 'manual', 'pending_review', :u)"
            ),
            {"u": typist},
        )
        await conn.execute(
            text("INSERT INTO admin_users (user_id, admin_role) VALUES (:u, 'super_admin')"),
            {"u": typist},
        )
    await _migrated(engine)
    return typist


async def test_every_table_is_kept_or_cleared_and_nothing_else(db_engine: AsyncEngine) -> None:
    assert await _tables(db_engine) == CLEAR | KEEP | set(CATALOGUE)
    assert not CLEAR & KEEP
    assert not CLEAR & set(CATALOGUE)
    assert NOT_ACTIVITY <= CLEAR
    assert set(LINK_TABLES) <= CLEAR


async def test_apply_clears_every_cleared_table_and_keeps_every_kept_row(
    db_engine: AsyncEngine,
) -> None:
    await seed_rehearsal(db_engine)
    before = await _counts(db_engine, set(CLEAR))
    kept = await _checksums(db_engine)
    # Not vacuous: the seed reached most of the schema, and every link table.
    assert sum(1 for n in before.values() if n) >= 30
    assert all(before[t] for t in LINK_TABLES)

    outcome = await reset(db_engine, real_data_from=LATER, apply=True, remove_file=_gone)

    assert outcome.applied
    assert outcome.refused == ()
    assert outcome.survey.clear == before
    assert outcome.survey.user_created == {"institutions": 1, "scholarship_programs": 0}
    assert set((await _counts(db_engine, set(CLEAR))).values()) == {0}
    assert await _checksums(db_engine) == kept
    assert kept["institutions"][0] == 1  # the registry school stayed, the typed one went
    async with db_engine.connect() as conn:
        left = await conn.execute(text("SELECT name FROM institutions"))
        assert [row[0] for row in left] == ["Registry University"]


async def test_the_link_tracking_tables_are_emptied(db_engine: AsyncEngine) -> None:
    await seed_rehearsal(db_engine)
    assert all((await _counts(db_engine, set(LINK_TABLES))).values())

    await reset(db_engine, real_data_from=LATER, apply=True, remove_file=_gone)

    assert await _counts(db_engine, set(LINK_TABLES)) == dict.fromkeys(sorted(LINK_TABLES), 0)


async def test_a_dry_run_writes_nothing(db_engine: AsyncEngine) -> None:
    await seed_rehearsal(db_engine)
    tables = await _tables(db_engine)
    before = await _counts(db_engine, tables)
    kept = await _checksums(db_engine)

    outcome = await reset(db_engine, real_data_from=LATER, apply=False, remove_file=_gone)

    assert not outcome.applied
    assert outcome.refused == ()
    assert outcome.survey.clear["users"] == before["users"] > 0
    assert await _counts(db_engine, tables) == before
    assert await _checksums(db_engine) == kept


async def test_running_it_twice_is_safe(db_engine: AsyncEngine) -> None:
    await seed_rehearsal(db_engine)
    await reset(db_engine, real_data_from=LATER, apply=True, remove_file=_gone)
    kept = await _checksums(db_engine)

    again = await reset(db_engine, real_data_from=LATER, apply=True, remove_file=_gone)

    assert again.applied
    assert set(again.survey.clear.values()) == {0}
    assert await _checksums(db_engine) == kept


async def test_a_reloaded_account_relinks_to_its_existing_sign_in(db_engine: AsyncEngine) -> None:
    """provision_auth links by address: the old row's auth_id is free to take again."""
    sign_in = uuid4()
    async with db_engine.begin() as conn:
        await add_user(conn, "ada@example.test", auth_id=sign_in, legacy_bubble_id="b1")
    await _migrated(db_engine)
    await reset(db_engine, real_data_from=LATER, apply=True, remove_file=_gone)
    async with db_engine.begin() as conn:
        reloaded = await add_user(conn, "ada@example.test", legacy_bubble_id="b1")

    store = ProvisioningStore(db_engine)
    [candidate] = await store.candidates()
    plan = plan_for(candidate, lambda email: sign_in if email == "ada@example.test" else None)
    assert candidate == Candidate(user_id=reloaded, email="ada@example.test", auth_id=None)
    assert plan.action is Action.LINK
    assert await store.link(reloaded, sign_in)


async def test_a_sign_up_on_the_new_app_refuses_and_deletes_nothing(db_engine: AsyncEngine) -> None:
    await seed_rehearsal(db_engine)
    async with db_engine.begin() as conn:
        await add_user(conn, f"new-{uuid4()}@example.test")
    before = await _counts(db_engine, set(CLEAR))

    outcome = await reset(db_engine, real_data_from=LATER, apply=True, remove_file=_gone)

    assert not outcome.applied
    assert outcome.survey.new_accounts == 1
    assert any("signed up on the new app" in reason for reason in outcome.refused)
    assert await _counts(db_engine, set(CLEAR)) == before


async def test_our_own_new_accounts_are_not_real_users(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        admin = await add_user(conn, f"admin-{uuid4()}@example.test")
        await conn.execute(
            text("INSERT INTO admin_users (user_id, admin_role) VALUES (:u, 'super_admin')"),
            {"u": admin},
        )

    outcome = await reset(db_engine, real_data_from=LATER, apply=True, remove_file=_gone)

    assert outcome.applied
    assert (outcome.survey.new_accounts, outcome.survey.internal_accounts) == (0, 1)


async def test_activity_after_the_cutoff_refuses_and_names_the_tables(
    db_engine: AsyncEngine,
) -> None:
    await seed_rehearsal(db_engine)
    before = await _counts(db_engine, set(CLEAR))

    outcome = await reset(db_engine, real_data_from=EARLIER, apply=True, remove_file=_gone)

    assert not outcome.applied
    assert outcome.survey.recent["sessions"] > 0
    assert outcome.survey.recent["mentor_profile_visits"] > 0
    assert any(reason.startswith("sessions: ") for reason in outcome.refused)
    assert not any(reason.startswith("visitor_day_keys") for reason in outcome.refused)
    assert await _counts(db_engine, set(CLEAR)) == before


async def test_a_job_writing_after_the_cutoff_is_not_activity(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO visitor_day_keys (day, key) VALUES (:d, :k)"),
            {"d": NOW.date(), "k": os.urandom(32)},
        )

    outcome = await reset(db_engine, real_data_from=EARLIER, apply=True, remove_file=_gone)

    assert outcome.applied


async def test_a_table_nobody_classified_refuses(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        await conn.execute(text("CREATE TABLE stray (id int)"))

    outcome = await reset(db_engine, real_data_from=LATER, apply=True, remove_file=_gone)

    assert not outcome.applied
    assert outcome.survey.unknown_tables == ("stray",)


async def test_a_kept_row_pointing_at_a_cleared_user_refuses(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        approver = await add_user(conn, f"approver-{uuid4()}@example.test")
        await conn.execute(
            text(
                "UPDATE scholarship_programs SET approved_by = :u "
                "WHERE id = (SELECT id FROM scholarship_programs ORDER BY id LIMIT 1)"
            ),
            {"u": approver},
        )
    await _migrated(db_engine)

    outcome = await reset(db_engine, real_data_from=LATER, apply=True, remove_file=_gone)

    assert not outcome.applied
    assert outcome.survey.blocked["scholarship_programs.approved_by"] == 1


async def test_a_kept_row_merged_into_a_typed_school_refuses(db_engine: AsyncEngine) -> None:
    await seed_rehearsal(db_engine)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE institutions SET merged_into_id = "
                "(SELECT id FROM institutions WHERE created_by IS NOT NULL) "
                "WHERE created_by IS NULL"
            )
        )

    outcome = await reset(db_engine, real_data_from=LATER, apply=True, remove_file=_gone)

    assert not outcome.applied
    assert {k: n for k, n in outcome.survey.blocked.items() if n} == {
        "institutions.merged_into_id": 1
    }


async def test_the_database_must_be_named(db_engine: AsyncEngine) -> None:
    await seed_rehearsal(db_engine)
    dsn = db_engine.url.render_as_string(hide_password=False)
    name = db_engine.url.database
    assert name

    assert (
        await run(dsn, confirm="someone-else", apply=True, real_data_from=LATER, remove_file=_gone)
        == EXIT_REFUSED
    )
    assert (await _counts(db_engine, {"users"}))["users"] > 0

    assert (
        await run(dsn, confirm=name, apply=True, real_data_from=LATER, remove_file=_gone) == EXIT_OK
    )
    assert (await _counts(db_engine, {"users"}))["users"] == 0


async def test_a_naive_cutoff_is_refused(db_engine: AsyncEngine) -> None:
    dsn = db_engine.url.render_as_string(hide_password=False)
    naive = LATER.replace(tzinfo=None)
    assert (
        await run(
            dsn,
            confirm=str(db_engine.url.database),
            apply=False,
            real_data_from=naive,
            remove_file=_gone,
        )
        == EXIT_REFUSED
    )


async def test_rows_a_trigger_puts_back_are_caught(db_engine: AsyncEngine) -> None:
    """Deleting weekly hours logs an availability change; in the wrong order it stays."""
    await make_bookable_mentor(db_engine, "trigger")
    async with db_engine.connect() as conn, conn.begin():
        with pytest.raises(ResetVerificationError, match="mentor_availability_changes"):
            await reset_module._delete(
                conn, ("mentor_availability_changes", "availability_rules"), kept=[]
            )


async def test_a_kept_table_that_changed_rolls_everything_back(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed_rehearsal(db_engine)
    before = await _counts(db_engine, set(CLEAR))
    readings = iter([{"countries": (1, "a")}, {"countries": (1, "b")}])

    async def drifting(_conn: object, _tables: object) -> dict[str, tuple[int, str]]:
        return next(readings)

    monkeypatch.setattr(reset_module, "fingerprints", drifting)

    with pytest.raises(ResetVerificationError, match="kept table changed"):
        await reset(db_engine, real_data_from=LATER, apply=True, remove_file=_gone)
    assert await _counts(db_engine, set(CLEAR)) == before


async def test_a_server_on_another_database_refuses(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The name typed must be the database the server actually opened."""
    dsn = db_engine.url.render_as_string(hide_password=False)
    monkeypatch.setattr(
        reset_module,
        "target_of",
        lambda _dsn: Target(host="localhost", database="elsewhere", user="edufurther"),
    )
    async with db_engine.begin() as conn:
        await add_user(conn, f"kept-{uuid4()}@example.test")
    await _migrated(db_engine)

    assert (
        await run(dsn, confirm="elsewhere", apply=True, real_data_from=LATER, remove_file=_gone)
        == EXIT_REFUSED
    )
    assert (await _counts(db_engine, {"users"}))["users"] == 1


async def test_private_files_are_removed_after_the_rows(db_engine: AsyncEngine) -> None:
    await seed_rehearsal(db_engine)
    removed: list[str] = []

    def bucket(key: str) -> bool:
        removed.append(key)
        return True

    outcome = await reset(db_engine, real_data_from=LATER, apply=True, remove_file=bucket)

    assert outcome.survey.stored_files == SEEDED_FILES
    assert len(removed) == SEEDED_FILES
    assert any(key.startswith("messages/") for key in removed)
    assert not any(key.endswith("/gone") for key in removed)
    assert outcome.files_left == ()


async def test_a_file_storage_refused_is_listed_and_the_reset_stands(
    db_engine: AsyncEngine,
) -> None:
    await seed_rehearsal(db_engine)

    def failing(key: str) -> bool:
        raise StorageError(f"delete of {key} failed with 503")

    dsn = db_engine.url.render_as_string(hide_password=False)
    code = await run(
        dsn,
        confirm=str(db_engine.url.database),
        apply=True,
        real_data_from=LATER,
        remove_file=failing,
    )

    assert code == EXIT_UNRESOLVED
    assert (await _counts(db_engine, {"users"}))["users"] == 0


async def test_files_with_no_bucket_to_remove_them_from_refuse(db_engine: AsyncEngine) -> None:
    await seed_rehearsal(db_engine)
    before = await _counts(db_engine, set(CLEAR))

    outcome = await reset(db_engine, real_data_from=LATER, apply=True, remove_file=None)

    assert not outcome.applied
    assert any("private file(s)" in reason for reason in outcome.refused)
    assert await _counts(db_engine, set(CLEAR)) == before


async def test_a_later_cutoff_than_the_default_is_refused(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        await add_user(conn, f"kept-{uuid4()}@example.test")
    await _migrated(db_engine)
    dsn = db_engine.url.render_as_string(hide_password=False)
    later = CAP + dt.timedelta(seconds=1)

    code = await run(dsn, confirm=str(db_engine.url.database), apply=True, real_data_from=later)

    assert code == EXIT_REFUSED
    assert (await _counts(db_engine, {"users"}))["users"] == 1


async def test_a_lock_it_cannot_take_refuses_and_writes_nothing(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with db_engine.begin() as conn:
        await add_user(conn, f"kept-{uuid4()}@example.test")
    await _migrated(db_engine)
    monkeypatch.setattr(reset_module, "LOCK_TIMEOUT", "200ms")
    dsn = db_engine.url.render_as_string(hide_password=False)

    async with db_engine.connect() as holder, holder.begin():
        await holder.execute(text("LOCK TABLE users IN ROW EXCLUSIVE MODE"))
        code = await run(dsn, confirm=str(db_engine.url.database), apply=True, real_data_from=LATER)

    assert code == EXIT_REFUSED
    assert (await _counts(db_engine, {"users"}))["users"] == 1


async def test_a_cleared_table_with_no_creation_time_is_reported_not_fatal(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with db_engine.begin() as conn:
        await conn.execute(text("CREATE TABLE untimed_join (id int)"))
    monkeypatch.setattr(reset_module, "CLEAR", CLEAR | {"untimed_join"})

    outcome = await reset(db_engine, real_data_from=EARLIER, apply=False, remove_file=_gone)

    assert outcome.refused == ()
    assert outcome.survey.untimed == ("untimed_join",)


@pytest.mark.parametrize(("apply", "read_only"), [(False, "on"), (True, "off")])
async def test_the_survey_reads_one_snapshot(
    db_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch, apply: bool, read_only: str
) -> None:
    """REPEATABLE READ, so the counts in one report cannot contradict each other."""
    seen: dict[str, str] = {}
    real_survey = reset_module.survey

    async def spy(conn: AsyncConnection, **kwargs: Any) -> Any:
        for setting in ("transaction_isolation", "transaction_read_only"):
            seen[setting] = (await conn.execute(text(f"SHOW {setting}"))).scalar_one()
        return await real_survey(conn, **kwargs)

    monkeypatch.setattr(reset_module, "survey", spy)

    await reset(db_engine, real_data_from=LATER, apply=apply, remove_file=_gone)

    assert seen == {"transaction_isolation": "repeatable read", "transaction_read_only": read_only}
