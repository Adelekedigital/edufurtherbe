"""What the conferencing-venue migrations leave behind, beyond "the chain runs".

Split from `test_migrations.py` by area when it reached the file-size limit
(non-negotiable 11); the helpers are imported from there, not copied.
"""

import pytest
from alembic import command
from tests.integration.test_migrations import (
    MENTOR_WITH_OFFERING,
    ConfigFactory,
    execute,
    scalar,
)

pytestmark = pytest.mark.db


_OPTION = """
    INSERT INTO mentor_conferencing_options (user_id, provider, is_default)
    SELECT id, '{provider}', {default} FROM users WHERE email = '{email}'
"""

_POINT = """
    UPDATE session_types st SET conferencing_option_id = o.id
      FROM mentor_conferencing_options o, users u
     WHERE u.email = '{email}' AND st.mentor_user_id = u.id
       AND o.user_id = u.id AND o.provider = '{provider}'
"""

_VENUES = """
    SELECT string_agg(
        u.email || '=' || coalesce((
            SELECT string_agg(
                o.provider::text || (CASE WHEN o.is_default THEN '*' ELSE '' END),
                '+' ORDER BY o.provider::text)
              FROM mentor_conferencing_options o WHERE o.user_id = u.id), '-')
        || '>' || coalesce((
            SELECT o.provider::text FROM session_types st
              JOIN mentor_conferencing_options o ON o.id = st.conferencing_option_id
             WHERE st.mentor_user_id = u.id), 'none'),
        ',' ORDER BY u.email)
      FROM users u WHERE u.email LIKE 'venue-%'
"""


def _venue_probes(url: str) -> None:
    """A never chose (an imported `google_meet` default). B chose daily. C has a
    daily default plus an imported `google_meet` its offering points at."""
    for email in ("venue-a@example.test", "venue-b@example.test", "venue-c@example.test"):
        execute(
            url, MENTOR_WITH_OFFERING.format(email=email, name=email, stage="NULL", label="NULL")
        )
    for email, provider, default in (
        ("venue-a@example.test", "google_meet", "true"),
        ("venue-b@example.test", "daily", "true"),
        ("venue-c@example.test", "daily", "true"),
        ("venue-c@example.test", "google_meet", "false"),
    ):
        execute(url, _OPTION.format(email=email, provider=provider, default=default))
    for email, provider in (
        ("venue-a@example.test", "google_meet"),
        ("venue-b@example.test", "daily"),
        ("venue-c@example.test", "google_meet"),
    ):
        execute(url, _POINT.format(email=email, provider=provider))


def test_imported_meet_options_go_so_nobody_is_held_to_a_choice_they_never_made(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """`a7c4e2d91f3b`: before `/me/conferencing` (#224) nothing let a mentor
    choose Meet, so every `google_meet` option was imported from a blank or an
    unrepresentable legacy venue. They go, with the pointers to them; a genuine
    `daily` choice stays."""
    config = make_alembic_config(disposable_database)
    command.upgrade(config, "f3a91d2c7b45")
    _venue_probes(disposable_database)
    assert scalar(disposable_database, _VENUES) == (
        "venue-a@example.test=google_meet*>google_meet,"
        "venue-b@example.test=daily*>daily,"
        "venue-c@example.test=daily*+google_meet>google_meet"
    )

    command.upgrade(config, "a7c4e2d91f3b")

    assert scalar(disposable_database, _VENUES) == (
        "venue-a@example.test=->none,"
        "venue-b@example.test=daily*>daily,"
        "venue-c@example.test=daily*>none"
    )


def test_a_meet_the_mentor_chose_survives_a_downgrade_and_upgrade(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """Provenance, not provider, decides what the cleanup removes: `source`
    records who wrote the row, so a Meet set through `/me/conferencing` is kept
    when `a7c4e2d91f3b` runs again after a downgrade, and an imported one goes."""
    config = make_alembic_config(disposable_database)
    command.upgrade(config, "a7c4e2d91f3b")
    for email in ("venue-chose@example.test", "venue-import@example.test"):
        execute(
            disposable_database,
            MENTOR_WITH_OFFERING.format(email=email, name=email, stage="NULL", label="NULL"),
        )
    execute(
        disposable_database,
        "INSERT INTO mentor_conferencing_options (user_id, provider, is_default, source) "
        "SELECT id, 'google_meet', true, 'mentor' FROM users "
        "WHERE email = 'venue-chose@example.test'; "
        "INSERT INTO mentor_conferencing_options (user_id, provider, is_default) "
        "SELECT id, 'google_meet', true FROM users WHERE email = 'venue-import@example.test'",
    )

    command.downgrade(config, "-1")
    command.upgrade(config, "a7c4e2d91f3b")

    assert (
        scalar(
            disposable_database,
            "SELECT string_agg(u.email || '=' || o.source, ',' ORDER BY u.email) "
            "FROM mentor_conferencing_options o JOIN users u ON u.id = o.user_id "
            "WHERE u.email LIKE 'venue-%'",
        )
        == "venue-chose@example.test=mentor"
    )


def test_the_imported_meet_downgrade_restores_the_old_load_rule(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """Down, a mentor left with no default gets the `google_meet` default the old
    load gave them, and their offering points at it again. A mentor who kept a
    default is left alone: which imported option the old seeding made default is
    not recoverable, and their offering follows that default."""
    config = make_alembic_config(disposable_database)
    command.upgrade(config, "f3a91d2c7b45")
    _venue_probes(disposable_database)
    command.upgrade(config, "a7c4e2d91f3b")

    command.downgrade(config, "f3a91d2c7b45")

    assert scalar(disposable_database, _VENUES) == (
        "venue-a@example.test=google_meet*>google_meet,"
        "venue-b@example.test=daily*>daily,"
        "venue-c@example.test=daily*>none"
    )


_PINS = """
    SELECT string_agg(
        u.email || '=' || coalesce((
            SELECT string_agg(o.provider::text || ':' || o.source::text
                              || (CASE WHEN o.is_default THEN '*' ELSE '' END),
                              '+' ORDER BY o.provider::text)
              FROM mentor_conferencing_options o WHERE o.user_id = u.id), '-')
        || '>' || coalesce((
            SELECT string_agg(coalesce(o.provider::text, 'none'), '+' ORDER BY st.name)
              FROM session_types st
              LEFT JOIN mentor_conferencing_options o ON o.id = st.conferencing_option_id
             WHERE st.mentor_user_id = u.id), '-'),
        ',' ORDER BY u.email)
      FROM users u WHERE u.email LIKE 'pin-%'
"""


_OPTIONS_FOR = """
    INSERT INTO mentor_conferencing_options (user_id, provider, is_default, source)
    SELECT u.id, v.p, v.d, v.s FROM users u, (VALUES {rows}) AS v(p, d, s)
     WHERE u.email = '{email}'
"""

_PIN_DAILY = """
    UPDATE session_types st SET conferencing_option_id = o.id
      FROM mentor_conferencing_options o, users u
     WHERE o.user_id = st.mentor_user_id AND u.id = o.user_id
       AND u.email = '{email}' AND o.provider = 'daily'
"""


def _pin_probes(url: str) -> None:
    """A: imported daily default, live offering pinned to it. B: the same row
    re-chosen through `/me/conferencing` (`source = 'mentor'`), still pinned. C:
    imported default, a *deleted* offering pinned to it. D: an imported daily
    left behind when the mentor chose Meet, and an unpinned offering. E: pinned
    to an imported Meet that is not the default, with no default at all. F: a
    Meet default they chose, offering pinned to an imported daily beside it. G:
    no default, only a *deleted* offering pinned to an imported daily."""
    for email in ("pin-a@example.test", "pin-b@example.test", "pin-c@example.test"):
        execute(
            url, MENTOR_WITH_OFFERING.format(email=email, name=email, stage="NULL", label="NULL")
        )
    execute(
        url,
        "INSERT INTO mentor_conferencing_options (user_id, provider, is_default, source) "
        "SELECT id, 'daily', true, CASE WHEN email = 'pin-b@example.test' "
        "THEN 'mentor' ELSE 'import' END FROM users WHERE email LIKE 'pin-%'; "
        "UPDATE session_types st SET conferencing_option_id = o.id "
        "FROM mentor_conferencing_options o "
        "WHERE o.user_id = st.mentor_user_id AND o.is_default; "
        "UPDATE session_types st SET deleted_at = now() FROM users u "
        "WHERE u.id = st.mentor_user_id AND u.email = 'pin-c@example.test'",
    )
    email = "pin-d@example.test"
    execute(url, MENTOR_WITH_OFFERING.format(email=email, name=email, stage="NULL", label="NULL"))
    execute(
        url,
        "INSERT INTO mentor_conferencing_options (user_id, provider, is_default, source) "
        "SELECT id, 'daily', false, 'import' FROM users WHERE email = 'pin-d@example.test' "
        "UNION ALL "
        "SELECT id, 'google_meet', true, 'mentor' FROM users WHERE email = 'pin-d@example.test'",
    )
    email = "pin-e@example.test"
    execute(url, MENTOR_WITH_OFFERING.format(email=email, name=email, stage="NULL", label="NULL"))
    execute(
        url,
        "INSERT INTO mentor_conferencing_options (user_id, provider, is_default, source) "
        "SELECT id, 'google_meet', false, 'import' FROM users WHERE email = 'pin-e@example.test'",
    )
    execute(
        url,
        "UPDATE session_types st SET conferencing_option_id = o.id "
        "FROM mentor_conferencing_options o, users u "
        "WHERE o.user_id = st.mentor_user_id AND u.id = o.user_id "
        "AND u.email = 'pin-e@example.test'",
    )
    for email, rows in (
        ("pin-f@example.test", "('daily', false, 'import'), ('google_meet', true, 'mentor')"),
        ("pin-g@example.test", "('daily', false, 'import')"),
    ):
        execute(
            url, MENTOR_WITH_OFFERING.format(email=email, name=email, stage="NULL", label="NULL")
        )
        execute(
            url,
            _OPTIONS_FOR.format(email=email, rows=rows),
        )
        execute(
            url,
            _PIN_DAILY.format(email=email),
        )
    execute(
        url,
        "UPDATE session_types st SET deleted_at = now() FROM users u "
        "WHERE u.id = st.mentor_user_id AND u.email = 'pin-g@example.test'",
    )


def test_imported_venue_pins_are_cleared_so_the_default_governs(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """`b2d6f9a1c4e7`: nothing but the legacy load ever wrote an offering's
    venue pointer, and no screen shows one, so a pinned offering ignored the
    mentor's later switch of default. Every pointer goes; every option row and
    default stays exactly as it was."""
    config = make_alembic_config(disposable_database)
    command.upgrade(config, "f4a7c2d9b318")
    _pin_probes(disposable_database)
    assert scalar(disposable_database, _PINS) == (
        "pin-a@example.test=daily:import*>daily,"
        "pin-b@example.test=daily:mentor*>daily,"
        "pin-c@example.test=daily:import*>daily,"
        "pin-d@example.test=daily:import+google_meet:mentor*>none,"
        "pin-e@example.test=google_meet:import>google_meet,"
        "pin-f@example.test=daily:import+google_meet:mentor*>daily,"
        "pin-g@example.test=daily:import>daily"
    )

    command.upgrade(config, "b2d6f9a1c4e7")

    assert scalar(disposable_database, _PINS) == (
        "pin-a@example.test=daily:import*>none,"
        "pin-b@example.test=daily:mentor*>none,"
        "pin-c@example.test=daily:import*>none,"
        "pin-d@example.test=daily:import+google_meet:mentor*>none,"
        "pin-e@example.test=google_meet:import*>none,"
        "pin-f@example.test=daily:import+google_meet:mentor*>none,"
        "pin-g@example.test=daily:import>none"
    )


def test_the_pin_downgrade_restores_the_old_load_rule(
    disposable_database: str, make_alembic_config: ConfigFactory
) -> None:
    """Down, an imported default gets its live offerings pointed at it again —
    what the old load wrote. A default the mentor re-chose is theirs and is left
    following the default, and a deleted offering stays unpointed: neither is
    recoverable, and each resolves to the same venue either way."""
    config = make_alembic_config(disposable_database)
    command.upgrade(config, "f4a7c2d9b318")
    _pin_probes(disposable_database)
    command.upgrade(config, "b2d6f9a1c4e7")

    command.downgrade(config, "f4a7c2d9b318")

    assert scalar(disposable_database, _PINS) == (
        "pin-a@example.test=daily:import*>daily,"
        "pin-b@example.test=daily:mentor*>none,"
        "pin-c@example.test=daily:import*>none,"
        "pin-d@example.test=daily:import+google_meet:mentor*>none,"
        "pin-e@example.test=google_meet:import*>google_meet,"
        "pin-f@example.test=daily:import+google_meet:mentor*>none,"
        "pin-g@example.test=daily:import>none"
    )
