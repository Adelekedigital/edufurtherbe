"""The reset's rules: which database is meant, the deletion order, and refusals."""

from __future__ import annotations

import datetime as dt

import pytest

from app.domain.app_reset import (
    DEFAULT_REAL_DATA_FROM,
    CycleError,
    Survey,
    Target,
    confirms,
    deletion_order,
    refusals,
    report,
)

REF = "abcdefghij0123456789"


def clean(**changes: object) -> Survey:
    base: dict[str, object] = {
        "real_data_from": DEFAULT_REAL_DATA_FROM,
        "clear": {"users": 3, "sessions": 2},
        "keep": {"countries": 249},
        "user_created": {"institutions": 1},
        "new_accounts": 0,
        "internal_accounts": 0,
        "recent": {"users": 0},
    }
    return Survey(**(base | changes))  # type: ignore[arg-type]


def test_the_default_cutoff_is_the_start_of_the_final_load_window() -> None:
    assert dt.datetime(2026, 11, 19, tzinfo=dt.UTC) == DEFAULT_REAL_DATA_FROM


@pytest.mark.parametrize(
    "target",
    [
        Target(host=f"db.{REF}.supabase.co", database="postgres", user="postgres"),
        Target(
            host="aws-0-eu-west-2.pooler.supabase.com", database="postgres", user=f"postgres.{REF}"
        ),
    ],
)
def test_supabase_is_named_by_its_project_ref(target: Target) -> None:
    assert target.project_ref == REF
    assert confirms(target, REF)
    assert confirms(target, f" {REF} ")
    assert not confirms(target, "postgres")
    assert not confirms(target, "bcdefghij0123456789a")


def test_elsewhere_the_database_name_names_it() -> None:
    target = Target(host="localhost", database="edufurther", user="edufurther")
    assert target.project_ref is None
    assert confirms(target, "edufurther")
    assert not confirms(target, "edufurther_test")
    assert not confirms(Target(host="localhost", database=None, user=None), "")


def test_the_description_names_no_password() -> None:
    target = Target(host=f"db.{REF}.supabase.co", database="postgres", user="postgres")
    assert target.describe() == f"host=db.{REF}.supabase.co database=postgres project={REF}"


def test_children_are_deleted_before_their_parents() -> None:
    references = [
        ("sessions", "users"),
        ("reviews", "sessions"),
        ("reviews", "users"),
        ("sessions", "sessions"),
        ("users", "countries"),
    ]
    order = deletion_order({"users", "sessions", "reviews"}, references)
    assert order == ("reviews", "sessions", "users")


def test_a_cycle_has_no_order() -> None:
    with pytest.raises(CycleError):
        deletion_order({"a", "b"}, [("a", "b"), ("b", "a")])


def test_a_clean_survey_is_not_refused() -> None:
    assert refusals(clean()) == ()


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"new_accounts": 2}, "2 account(s) signed up on the new app"),
        ({"recent": {"messages": 4}}, "messages: 4 row(s) created at or after 2026-11-19"),
        ({"unknown_tables": ("stray",)}, "neither kept nor cleared"),
        ({"unsafe_keys": ("fk_x",)}, "through fk_x"),
        ({"blocked": {"institutions.merged_into_id": 1}}, "institutions.merged_into_id: 1"),
        ({"cycle": "a -> b -> a"}, "cycle: a -> b -> a"),
    ],
)
def test_each_finding_refuses(changes: dict[str, object], expected: str) -> None:
    found = refusals(clean(**changes))
    assert len(found) == 1
    assert expected in found[0]


def test_internal_accounts_alone_do_not_refuse() -> None:
    assert refusals(clean(internal_accounts=3)) == ()


def test_the_report_lists_both_sides_with_counts() -> None:
    text = report(clean(), applied=False)
    assert "KEEP (untouched):" in text
    assert "CLEAR (would delete):" in text
    assert "countries" in text
    assert "institutions (user-created rows)" in text
    assert text.splitlines()[-1].split() == ["total", "6"]
    assert "CLEAR (deleted):" in report(clean(), applied=True)
