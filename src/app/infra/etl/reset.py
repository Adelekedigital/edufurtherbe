"""Clear rehearsal app data from a database, keeping reference data and sign-ins.

The rules (what is real data, which database is meant, the deletion order) are
``domain/app_reset.py``; this module classifies the tables, reads the facts and
deletes. ``scripts/reset_app_data.py`` only wires it up (settled decision #44).

WHAT IS KEPT
============
``KEEP`` is every table no loader or user writes: the Alembic version, the ISO
countries and languages, degree levels and the six service offerings (all
seeded by migrations), and the legal documents. ``CATALOGUE`` (institutions,
scholarship programmes) keeps its catalogue rows and clears the rows a *user*
created (``created_by`` set), because those point at accounts being cleared.
Supabase's ``auth.users`` is not ours and is never read; ``provision_auth.py``
relinks each reloaded account to its existing sign-in by address, so no
``users.auth_id`` has to survive. Stored images are not touched either.

WHAT IS CLEARED
===============
``CLEAR``: every table the loaders, the app or its jobs write, whole. A table
in the database that is in neither list **refuses the reset**, so a new table
cannot be cleared or kept by accident; ``test_app_reset`` pins the lists to
the migrated schema.

HOW
===
One transaction with a ``lock_timeout``. The cleared tables are locked against
writes and the kept ones against change, the guard is asked again under the
locks, then ordered ``DELETE``s run, children first. Not ``TRUNCATE``: it
refuses a table a kept table references (institutions and scholarship
programmes reference ``users``), and ``CASCADE`` would empty those catalogues.
Before committing, every cleared table must be empty and every kept table must
hash exactly as before; otherwise the transaction rolls back. Both runs are
``REPEATABLE READ``, so the report is one snapshot; a dry run is ``READ ONLY``,
so it cannot write. A database error (a lock or statement timeout) rolls back
and refuses.

PRIVATE FILES
=============
Intake and message uploads live in the private intake bucket, keyed by ids
only (decisions 208, 302). Their rows are cleared, so their objects are removed
after the commit, as the account purge does; a failure leaves the object and
is listed by key. With files to remove and no bucket configured, it refuses:
personal documents nothing points at would never be found again.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.domain.app_reset import (
    CycleError,
    Survey,
    Target,
    confirms,
    cutoff_problem,
    deletion_order,
    refusals,
    report,
)
from app.domain.enums import SignupSource
from app.infra.etl.cli import EXIT_OK, EXIT_REFUSED, EXIT_UNRESOLVED
from app.infra.storage.supabase import StorageError

#: Kept whole: reference and seeded data, and the migration chain's state.
KEEP: frozenset[str] = frozenset(
    {
        "alembic_version",
        "countries",
        "degree_levels",
        "languages",
        "legal_documents",
        "service_offerings",
    }
)

#: Catalogues: kept, except the rows a user created (the named column is set).
CATALOGUE: Mapping[str, str] = {
    "institutions": "created_by",
    "scholarship_programs": "created_by",
}

#: Cleared whole: everything the loaders, the app and its jobs write.
CLEAR: frozenset[str] = frozenset(
    {
        "admin_credit_grants",
        "admin_users",
        "analytics_consents",
        "auth_identities",
        "availability_exceptions",
        "availability_rules",
        "calendar_connections",
        "conversation_participants",
        "conversations",
        "credit_lots",
        "credit_transactions",
        "education_entries",
        # Admins' rewording of email lines: rows keyed to admin accounts that
        # are cleared (RESTRICT), so they cannot outlive them.
        "email_line_edits",
        "email_line_overrides",
        "feature_interest",
        "featured_mentors",
        "idempotency_keys",
        "intake_answers",
        "intake_files",
        "intake_form_questions",
        "intake_submissions",
        "invite_link_retired_codes",
        "invite_links",
        "mentee_goal_countries",
        "mentee_goal_needs",
        "mentee_goal_schools",
        "mentee_goals",
        "mentor_admin_notes",
        "mentor_availability_changes",
        "mentor_conferencing_options",
        "mentor_guide_countries",
        "mentor_next_availability",
        # Release 1 keyed visitors from DATABASE_URL (decision 347), so these
        # rows could be linked back to people: they must not survive.
        "mentor_profile_shares",
        "mentor_profile_visits",
        "mentor_profiles",
        "mentor_service_offerings",
        "mentor_status_events",
        "message_attachments",
        "messages",
        "notification_channel_preferences",
        "outbox_events",
        "preview_events",
        "preview_sessions",
        "push_subscriptions",
        "referral_unlocks",
        "referrals",
        "review_reports",
        "reviews",
        "session_events",
        "session_missed_reasons",
        "session_participants",
        "session_room_stays",
        "session_suggestions",
        "session_type_booking_configs",
        "session_type_next_availability",
        "session_type_offerings",
        "session_type_question_options",
        "session_type_questions",
        "session_type_scheduling_windows",
        "session_type_stages",
        "session_types",
        "sessions",
        "user_activity_days",
        "user_awards",
        "user_dismissed_notices",
        "user_languages",
        "user_legal_consents",
        "user_links",
        "user_onboarding",
        "user_profiles",
        "users",
        # The day keys behind visitor keys (decision 348): no use after a reset.
        "visitor_day_keys",
        "writing_assist_requests",
    }
)

#: Cleared tables whose rows a job or a cache writes whatever anybody does, so
#: a recent row there says nothing about real activity.
NOT_ACTIVITY: frozenset[str] = frozenset(
    {
        "featured_mentors",
        "mentor_availability_changes",
        "mentor_next_availability",
        "outbox_events",
        "session_type_next_availability",
        "visitor_day_keys",
    }
)

LOCK_TIMEOUT = "10s"
STATEMENT_TIMEOUT = "300s"

_TABLES = text(
    "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition"
)
_KEYS = text(
    """
    SELECT child.relname, parent.relname, k.confdeltype, k.conname,
           ARRAY(SELECT a.attname FROM unnest(k.conkey) WITH ORDINALITY u(num, ord)
                 JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = u.num
                 ORDER BY u.ord)
    FROM pg_constraint k
    JOIN pg_class child ON child.oid = k.conrelid
    JOIN pg_class parent ON parent.oid = k.confrelid
    JOIN pg_namespace cn ON cn.oid = child.relnamespace
    JOIN pg_namespace pn ON pn.oid = parent.relnamespace
    WHERE k.contype = 'f' AND cn.nspname = 'public' AND pn.nspname = 'public'
    """
)

#: Tables carrying ``created_at``, which the time check reads.
_TIMED = text(
    "SELECT c.relname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND a.attname = 'created_at' AND NOT a.attisdropped"
)

#: Private objects the cleared rows point at: intake files not yet purged, and
#: message files (a sent or unsent row always has its object, decision 302).
_STORED_FILES = text(
    "SELECT storage_key FROM intake_files WHERE purged_at IS NULL "
    "UNION ALL SELECT storage_key FROM message_attachments"
)

#: Accounts that signed up on the new app: real ones, then our own (decision 329).
_NEW_ACCOUNTS = text(
    "SELECT count(*) FILTER (WHERE NOT is_internal), count(*) FILTER (WHERE is_internal) "
    "FROM users WHERE signup_source <> :migrated"
)


class ResetVerificationError(RuntimeError):
    """Raised inside the transaction so it rolls back: the result was not exact."""


@dataclass(frozen=True)
class Key:
    """One foreign key, as the catalog states it."""

    child: str
    parent: str
    action: str
    name: str
    columns: tuple[str, ...]


@dataclass(frozen=True)
class Outcome:
    survey: Survey
    refused: tuple[str, ...]
    applied: bool
    #: Storage keys (ids only) whose objects could not be removed after commit.
    files_left: tuple[str, ...] = ()


#: Removes one private object; ``False`` when it was already gone.
RemoveFile = Callable[[str], bool]


def target_of(dsn: str) -> Target:
    """Host, database and user of a DSN; never its password."""
    url = make_url(dsn)
    return Target(host=url.host, database=url.database, user=url.username)


def _q(name: str) -> str:
    """A quoted identifier.

    Every name passed here is a table in ``KEEP``, ``CATALOGUE`` or ``CLEAR``
    as the catalog reports it, or a column name the catalog reports: never
    input. That is why the statements below build SQL with f-strings (B608).
    """
    return '"' + name.replace('"', '""') + '"'


def _kept_rows(table: str) -> str:
    """The rows of a kept table that stay."""
    column = CATALOGUE.get(table)
    return f"{_q(column)} IS NULL" if column else "TRUE"


def _cleared_rows(table: str) -> str:
    """The rows of a cleared or catalogue table that go."""
    column = CATALOGUE.get(table)
    return f"{_q(column)} IS NOT NULL" if column else "TRUE"


async def _scalars(
    conn: AsyncConnection, queries: Mapping[str, str], **params: object
) -> dict[str, int]:
    """One statement for many counts, so they read one snapshot."""
    if not queries:
        return {}
    labels = list(queries)
    columns = ", ".join(f"({queries[label]}) AS c{i}" for i, label in enumerate(labels))
    row = (await conn.execute(text(f"SELECT {columns}"), params)).one()
    return {label: int(row[i]) for i, label in enumerate(labels)}


async def _keys(conn: AsyncConnection) -> list[Key]:
    rows = (await conn.execute(_KEYS)).all()
    return [Key(r[0], r[1], str(r[2]), r[3], tuple(r[4])) for r in rows]


async def _present(conn: AsyncConnection) -> set[str]:
    return {row[0] for row in (await conn.execute(_TABLES)).all()}


async def survey(
    conn: AsyncConnection,
    *,
    present: set[str],
    real_data_from: dt.datetime,
    storage_ready: bool,
) -> tuple[Survey, tuple[str, ...], tuple[str, ...]]:
    """Read every fact the guard and the report need.

    Returns the survey, the deletion order and the storage keys of the private
    files whose rows are cleared.
    """
    clear = sorted(CLEAR & present)
    catalogue = sorted(set(CATALOGUE) & present)
    keep = sorted(KEEP & present)
    unknown = tuple(sorted(present - CLEAR - KEEP - set(CATALOGUE)))
    keys = await _keys(conn)
    deleted_from = set(clear) | set(catalogue)

    unsafe: list[str] = []
    pointers: dict[str, str] = {}
    for key in keys:
        if key.parent not in deleted_from:
            continue
        if key.child in KEEP:
            unsafe.append(key.name)
        elif key.child in CATALOGUE:
            own = CATALOGUE[key.child]
            if len(key.columns) != 1 or (key.action in "cnd" and key.columns != (own,)):
                unsafe.append(key.name)
            elif key.columns != (own,):
                column = key.columns[0]
                where = f"{_kept_rows(key.child)} AND {_q(column)} IS NOT NULL"
                if key.parent in CATALOGUE:
                    # Only a pointer at a user-created row is cut.
                    where += (
                        f" AND {_q(column)} IN (SELECT id FROM {_q(key.parent)} "  # noqa: S608  # nosec B608
                        f"WHERE {_cleared_rows(key.parent)})"
                    )
                pointers[f"{key.child}.{column}"] = (
                    f"SELECT count(*) FROM {_q(key.child)} WHERE {where}"  # noqa: S608  # nosec B608
                )

    counts = await _scalars(
        conn,
        {
            **{f"clear:{t}": f"SELECT count(*) FROM {_q(t)}" for t in clear},  # noqa: S608  # nosec B608
            **{
                f"keep:{t}": f"SELECT count(*) FROM {_q(t)} WHERE {_kept_rows(t)}"  # noqa: S608  # nosec B608
                for t in [*keep, *catalogue]
            },
            **{
                f"user:{t}": f"SELECT count(*) FROM {_q(t)} WHERE {_cleared_rows(t)}"  # noqa: S608  # nosec B608
                for t in catalogue
            },
            **{f"blocked:{label}": query for label, query in pointers.items()},
        },
    )
    timed = {row[0] for row in (await conn.execute(_TIMED)).all()}
    activity = [t for t in clear if t not in NOT_ACTIVITY]
    recent = await _scalars(
        conn,
        {
            **{
                t: f"SELECT count(*) FROM {_q(t)} WHERE created_at >= :since"  # noqa: S608  # nosec B608
                for t in activity
                if t in timed
            },
            **{
                f"{t} (user-created)": f"SELECT count(*) FROM {_q(t)} "  # noqa: S608  # nosec B608
                f"WHERE {_cleared_rows(t)} AND created_at >= :since"
                for t in catalogue
                if t in timed
            },
        },
        since=real_data_from,
    )
    files: tuple[str, ...] = ()
    if {"intake_files", "message_attachments"} <= present:
        files = tuple(row[0] for row in (await conn.execute(_STORED_FILES)).all())
    new_accounts = internal_accounts = 0
    if "users" in present:
        row = (await conn.execute(_NEW_ACCOUNTS, {"migrated": SignupSource.MIGRATED.value})).one()
        new_accounts, internal_accounts = int(row[0]), int(row[1])

    order: tuple[str, ...] = ()
    cycle = None
    try:
        order = deletion_order(deleted_from, ((k.child, k.parent) for k in keys))
    except CycleError as error:
        cycle = " -> ".join(str(node) for node in error.args[1])

    def pick(prefix: str) -> dict[str, int]:
        return {k.split(":", 1)[1]: v for k, v in counts.items() if k.startswith(prefix)}

    found = Survey(
        real_data_from=real_data_from,
        clear=pick("clear:"),
        keep=pick("keep:"),
        user_created=pick("user:"),
        new_accounts=new_accounts,
        internal_accounts=internal_accounts,
        recent=recent,
        unknown_tables=unknown,
        unsafe_keys=tuple(sorted(unsafe)),
        blocked=pick("blocked:"),
        cycle=cycle,
        stored_files=len(files),
        storage_ready=storage_ready,
        untimed=tuple(t for t in activity if t not in timed),
    )
    return found, order, files


async def fingerprints(conn: AsyncConnection, tables: Iterable[str]) -> dict[str, tuple[int, str]]:
    """Row count and an md5 over every kept row, per kept table."""
    result: dict[str, tuple[int, str]] = {}
    for table in tables:
        row = (
            await conn.execute(
                text(
                    f"SELECT count(*), md5(coalesce(string_agg(t::text, E'\\n' "  # noqa: S608  # nosec B608
                    f"ORDER BY t::text), '')) FROM {_q(table)} t WHERE {_kept_rows(table)}"
                )
            )
        ).one()
        result[table] = (int(row[0]), str(row[1]))
    return result


async def reset(
    engine: AsyncEngine,
    *,
    real_data_from: dt.datetime,
    apply: bool,
    remove_file: RemoveFile | None = None,
) -> Outcome:
    """Survey, and with ``apply`` delete, in one transaction. Commits only a clean reset.

    ``REPEATABLE READ``, so every count in the report is one snapshot (taken
    after the locks on an apply). The private objects the cleared rows named
    are removed after the commit, as the account purge does (decision 277):
    a failed removal is reported by key and never undoes the reset.
    """
    async with engine.connect() as conn:
        transaction = await conn.begin()
        try:
            mode = "READ WRITE" if apply else "READ ONLY"
            await conn.execute(text(f"SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, {mode}"))
            await conn.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
            await conn.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))
            present = await _present(conn)
            if apply:
                await _lock(conn, present)
            found, order, files = await survey(
                conn,
                present=present,
                real_data_from=real_data_from,
                storage_ready=remove_file is not None,
            )
            refused = refusals(found)
            if refused or not apply:
                await transaction.rollback()
                return Outcome(found, refused, applied=False)
            await _delete(conn, order, kept=sorted(found.keep))
            await transaction.commit()
        except BaseException:
            if transaction.is_active:
                await transaction.rollback()
            raise
    left = tuple([key for key in files if not await _removed(remove_file, key)])
    return Outcome(found, (), applied=True, files_left=left)


async def _removed(remove_file: RemoveFile | None, key: str) -> bool:
    """Whether an object is gone now (removed, or already absent)."""
    if remove_file is None:
        return False
    try:
        await asyncio.to_thread(remove_file, key)
    except StorageError:
        return False
    return True


async def _delete(conn: AsyncConnection, order: tuple[str, ...], *, kept: list[str]) -> None:
    """Delete children first, then prove nothing is left and nothing kept moved."""
    before = await fingerprints(conn, kept)
    for table in order:
        await conn.execute(text(f"DELETE FROM {_q(table)} WHERE {_cleared_rows(table)}"))  # noqa: S608  # nosec B608
    left = await _scalars(
        conn,
        {t: f"SELECT count(*) FROM {_q(t)} WHERE {_cleared_rows(t)}" for t in order},  # noqa: S608  # nosec B608
    )
    if any(left.values()):
        raise ResetVerificationError(
            "rows left after the reset: "
            + ", ".join(f"{t} ({n})" for t, n in sorted(left.items()) if n)
        )
    if await fingerprints(conn, kept) != before:
        raise ResetVerificationError("a kept table changed during the reset")


async def _lock(conn: AsyncConnection, present: set[str]) -> None:
    """Hold off writers: to cleared tables (EXCLUSIVE) and kept ones (SHARE)."""
    writes = sorted((CLEAR | set(CATALOGUE)) & present)
    kept = sorted(KEEP & present)
    if writes:
        await conn.execute(text(f"LOCK TABLE {', '.join(map(_q, writes))} IN EXCLUSIVE MODE"))
    if kept:
        await conn.execute(text(f"LOCK TABLE {', '.join(map(_q, kept))} IN SHARE MODE"))


async def run(
    dsn: str,
    *,
    confirm: str,
    apply: bool,
    real_data_from: dt.datetime,
    remove_file: RemoveFile | None = None,
) -> int:
    """The whole command: check the target, survey, maybe delete, report. An exit code."""
    target = target_of(dsn)
    print(f"target: {target.describe()}")
    if problem := cutoff_problem(real_data_from):
        print(f"refused: {problem}; nothing written", file=sys.stderr)
        return EXIT_REFUSED
    if not confirms(target, confirm):
        print(
            "refused: --confirm-database does not name this database "
            "(the project ref on Supabase, else the database name); nothing written",
            file=sys.stderr,
        )
        return EXIT_REFUSED

    engine = create_async_engine(dsn)
    try:
        async with engine.connect() as conn:
            connected = (await conn.execute(text("SELECT current_database()"))).scalar_one()
        if connected != target.database:
            print(
                "refused: the server is not on the named database; nothing written", file=sys.stderr
            )
            return EXIT_REFUSED
        try:
            outcome = await reset(
                engine, real_data_from=real_data_from, apply=apply, remove_file=remove_file
            )
        except ResetVerificationError as error:
            print(f"refused: {error}; rolled back, nothing written", file=sys.stderr)
            return EXIT_REFUSED
        except DBAPIError as error:
            # A lock or statement timeout, or the server going away: the class
            # names which, and the message (which can quote SQL) is not printed.
            name = type(error.orig).__name__
            message = f"refused: database error ({name}); rolled back, nothing written"
            print(message, file=sys.stderr)
            return EXIT_REFUSED
    finally:
        await engine.dispose()

    print()
    print(report(outcome.survey, applied=outcome.applied))
    if outcome.refused:
        print("\nrefused, nothing written:", file=sys.stderr)
        for reason in outcome.refused:
            print(f"  - {reason}", file=sys.stderr)
        return EXIT_REFUSED
    if outcome.files_left:
        # Keys are ids only (decision 208), so naming them names nobody.
        print(f"\n{len(outcome.files_left)} private file(s) left in storage:", file=sys.stderr)
        for key in outcome.files_left:
            print(f"  {key}", file=sys.stderr)
        print("\napplied: rehearsal data cleared; remove those objects by hand")
        return EXIT_UNRESOLVED
    print("\napplied: rehearsal data cleared" if outcome.applied else "\ndry run: nothing written")
    return EXIT_OK
