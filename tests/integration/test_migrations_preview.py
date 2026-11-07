"""What `ebd36a4e11df` leaves behind, beyond "the chain runs" (decision 309).

Split from `test_migrations.py` by area, as the file-size limit asks
(non-negotiable 11); the helpers are imported from there, not copied.
"""

import pytest
from alembic import command
from tests.integration.test_migrations import ConfigFactory, execute, scalar

from app.domain.preview import PAGE_PATH_MAX_LENGTH, REVEAL_REASON_MAX_LENGTH

pytestmark = pytest.mark.db

REVISION = "ebd36a4e11df"
PREVIOUS = "8c4e1f7a2d93"

ADMIN = "00000000-0000-7000-8000-000000000001"
TARGET = "00000000-0000-7000-8000-000000000002"
SIGN_IN = "00000000-0000-7000-8000-0000000000a1"
OTHER_SIGN_IN = "00000000-0000-7000-8000-0000000000a2"
PREVIEW = "00000000-0000-7000-8000-0000000000b1"

#: An admin and a user to preview, and one live preview, built in SQL: the
#: app's factories need head.
SEED_TEMPLATE = """
INSERT INTO users (id, auth_id, email, primary_role, timezone) VALUES
  ('{admin}', gen_random_uuid(), 'a@example.test', 'mentee', 'UTC'),
  ('{target}', gen_random_uuid(), 't@example.test', 'mentor', 'UTC');
INSERT INTO preview_sessions (id, admin_user_id, auth_session_id, target_user_id)
VALUES ('{preview}', '{admin}', '{sign_in}', '{target}');
"""
SEED = SEED_TEMPLATE.format(admin=ADMIN, target=TARGET, preview=PREVIEW, sign_in=SIGN_IN)
COUNT_STARTED = (
    "SELECT count(*) FROM preview_sessions WHERE admin_user_id = '{}' AND id IS NOT NULL "
    "AND started_at IS NOT NULL AND target_since IS NOT NULL AND last_active_at IS NOT NULL"
)
END_PREVIEW = "UPDATE preview_sessions SET ended_at = now(), end_reason = 'exit' WHERE id = '{}'"
DELETE_USER = "DELETE FROM users WHERE id = '{}'"

SESSION_ROW = (
    "INSERT INTO preview_sessions (admin_user_id, auth_session_id, target_user_id, "
    "ended_at, end_reason) VALUES ('{}', '{}', '{}', {}, {})"
)
EVENT_ROW = (
    "INSERT INTO preview_events (session_id, admin_user_id, target_user_id, kind, path, "
    "method, endpoint, field_type, reason, end_reason) "
    "VALUES ('{session}', '{admin}', '{target}', '{kind}', {path}, {method}, {endpoint}, "
    "{field_type}, {reason}, {end_reason})"
)
DETAIL = ("path", "method", "endpoint", "field_type", "reason", "end_reason")

#: The detail each kind must carry, and nothing else.
VALID = {
    "start": {},
    "switch": {},
    "page_view": {"path": "'/mentor/home'"},
    "blocked_write": {"method": "'POST'", "endpoint": "'/api/v1/me/links'"},
    "reveal": {"field_type": "'messages'", "reason": "'Support ticket 12'"},
    "end": {"end_reason": "'exit'"},
}


def session(
    *,
    admin: str = ADMIN,
    sign_in: str = OTHER_SIGN_IN,
    target: str = TARGET,
    ended_at: str = "NULL",
    end_reason: str = "NULL",
) -> str:
    return SESSION_ROW.format(admin, sign_in, target, ended_at, end_reason)


def event(kind: str, **detail: str) -> str:
    values = dict.fromkeys(DETAIL, "NULL") | detail
    return EVENT_ROW.format(session=PREVIEW, admin=ADMIN, target=TARGET, kind=kind, **values)


def upgraded(url: str, config_factory: ConfigFactory) -> None:
    command.upgrade(config_factory(url), REVISION)
    execute(url, SEED)


def test_a_session_ends_with_a_reason_and_never_previews_its_admin(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    upgraded(disposable_database, make_alembic_config)

    execute(disposable_database, session(ended_at="now()", end_reason="'idle'"))
    assert (
        scalar(
            disposable_database,
            COUNT_STARTED.format(ADMIN),
        )
        == 2
    )
    with pytest.raises(Exception, match="ck_preview_sessions_ended_with_reason"):
        execute(disposable_database, session(ended_at="now()"))
    with pytest.raises(Exception, match="ck_preview_sessions_ended_with_reason"):
        execute(disposable_database, session(end_reason="'exit'"))
    with pytest.raises(Exception, match="ck_preview_sessions_end_reason_is_known"):
        execute(disposable_database, session(ended_at="now()", end_reason="'timeout'"))
    with pytest.raises(Exception, match="ck_preview_sessions_not_self"):
        execute(disposable_database, session(target=ADMIN))


def test_one_live_session_per_sign_in(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """A second live preview in the same sign-in is refused; another sign-in, or
    the first one ended, is not."""
    upgraded(disposable_database, make_alembic_config)

    with pytest.raises(Exception, match="uq_preview_sessions_live"):
        execute(disposable_database, session(sign_in=SIGN_IN))

    execute(disposable_database, session(sign_in=OTHER_SIGN_IN))
    execute(
        disposable_database,
        END_PREVIEW.format(PREVIEW),
    )
    execute(disposable_database, session(sign_in=SIGN_IN))

    live = "SELECT count(*) FROM preview_sessions WHERE ended_at IS NULL"
    assert scalar(disposable_database, live) == 2


def test_each_event_kind_carries_its_own_detail_and_only_it(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    upgraded(disposable_database, make_alembic_config)

    for kind, detail in VALID.items():
        execute(disposable_database, event(kind, **detail))
    assert scalar(disposable_database, "SELECT count(DISTINCT kind) FROM preview_events") == 6

    refused = [
        ("ck_preview_events_kind_is_known", event("login")),
        ("ck_preview_events_page_view_detail", event("page_view")),
        ("ck_preview_events_page_view_detail", event("start", path="'/x'")),
        ("ck_preview_events_blocked_write_detail", event("blocked_write", method="'POST'")),
        ("ck_preview_events_blocked_write_detail", event("blocked_write", endpoint="'/x'")),
        ("ck_preview_events_blocked_write_detail", event("switch", method="'POST'")),
        ("ck_preview_events_reveal_detail", event("reveal", field_type="'messages'")),
        ("ck_preview_events_reveal_detail", event("reveal", reason="'why'")),
        ("ck_preview_events_reveal_detail", event("start", reason="'why'")),
        ("ck_preview_events_end_detail", event("end")),
        ("ck_preview_events_end_detail", event("switch", end_reason="'exit'")),
        (
            "ck_preview_events_field_type_is_known",
            event("reveal", field_type="'files'", reason="'why'"),
        ),
        ("ck_preview_events_end_reason_is_known", event("end", end_reason="'timeout'")),
    ]
    for constraint, statement in refused:
        with pytest.raises(Exception, match=constraint):
            execute(disposable_database, statement)


def test_a_path_and_a_reason_are_bounded(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    upgraded(disposable_database, make_alembic_config)
    longest_path = "'/" + "p" * (PAGE_PATH_MAX_LENGTH - 1) + "'"
    longest_reason = "'" + "r" * REVEAL_REASON_MAX_LENGTH + "'"

    execute(disposable_database, event("page_view", path=longest_path))
    execute(disposable_database, event("reveal", field_type="'messages'", reason=longest_reason))

    with pytest.raises(Exception, match="ck_preview_events_path_length"):
        execute(disposable_database, event("page_view", path="''"))
    with pytest.raises(Exception, match="ck_preview_events_path_length"):
        execute(disposable_database, event("page_view", path=longest_path[:-1] + "p'"))
    with pytest.raises(Exception, match="ck_preview_events_reason_length"):
        execute(disposable_database, event("reveal", field_type="'messages'", reason="''"))
    with pytest.raises(Exception, match="ck_preview_events_reason_length"):
        execute(
            disposable_database,
            event("reveal", field_type="'messages'", reason=longest_reason[:-1] + "r'"),
        )


def test_the_constraints_name_the_bounds_the_code_reads(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """The migration spells the bounds out; this pins them to the constants the
    API and the model read, so the two cannot drift."""
    command.upgrade(make_alembic_config(disposable_database), REVISION)

    definition = (
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'ck_preview_events_{}'"
    )
    path = scalar(disposable_database, definition.format("path_length"))
    reason = scalar(disposable_database, definition.format("reason_length"))
    live = scalar(
        disposable_database,
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_preview_sessions_live'",
    )
    log = scalar(
        disposable_database,
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_preview_events_session_created'",
    )

    assert path.endswith(f"(char_length(path) <= {PAGE_PATH_MAX_LENGTH})))")
    assert reason.endswith(f"(char_length(reason) <= {REVEAL_REASON_MAX_LENGTH})))")
    assert live.startswith("CREATE UNIQUE INDEX")
    assert live.endswith("(admin_user_id, auth_session_id) WHERE (ended_at IS NULL)")
    assert log.endswith("(session_id, created_at)")


def test_nobody_is_deleted_out_from_under_a_preview_or_its_log(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    upgraded(disposable_database, make_alembic_config)
    execute(disposable_database, event("start"))

    with pytest.raises(Exception, match="fk_preview_events_session_id_preview_sessions"):
        execute(disposable_database, "DELETE FROM preview_sessions")
    execute(disposable_database, "DELETE FROM preview_events")
    for person in (ADMIN, TARGET):
        with pytest.raises(Exception, match="fk_preview_sessions_"):
            execute(disposable_database, DELETE_USER.format(person))


def test_the_event_log_holds_its_own_people(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """The log's own user keys restrict, as the session's do: a row names who
    did it and to whom."""
    upgraded(disposable_database, make_alembic_config)
    execute(disposable_database, event("start"))

    constraint = (
        "SELECT confdeltype::text FROM pg_constraint WHERE conname = 'fk_preview_events_{}_users'"
    )
    for column in ("admin_user_id", "target_user_id"):
        assert scalar(disposable_database, constraint.format(column)) == "r"


def test_updated_at_moves_on_a_session(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    upgraded(disposable_database, make_alembic_config)
    execute(
        disposable_database, "UPDATE preview_sessions SET updated_at = now() - interval '1 day'"
    )
    execute(disposable_database, "UPDATE preview_sessions SET last_active_at = last_active_at")

    moved = "SELECT bool_and(updated_at > now() - interval '1 hour') FROM preview_sessions"
    assert scalar(disposable_database, moved) is True


def test_the_downgrade_removes_the_tables_and_refuses_over_rows(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    config = make_alembic_config(disposable_database)
    upgraded(disposable_database, make_alembic_config)

    with pytest.raises(RuntimeError, match="preview_sessions is not empty"):
        command.downgrade(config, PREVIOUS)

    execute(disposable_database, "DELETE FROM preview_sessions")
    command.downgrade(config, PREVIOUS)

    for table in ("preview_sessions", "preview_events"):
        assert scalar(disposable_database, f"SELECT to_regclass('public.{table}')") is None
    command.upgrade(config, REVISION)
    assert scalar(disposable_database, "SELECT to_regclass('public.preview_events')") is not None
