"""The feature-key rule and its one home (#365).

`feature` is an **open slug rather than an enum**, deliberately: a closed
vocabulary would turn a client typo into a `422` and put a backend release in
front of every new coming-soon button, which is the thing this exists to remove.
Two buttons had already been cut for want of somewhere to record a press.

The cost of an open vocabulary is that the *shape* rule is the only rule there
is — so the shape has to mean the same thing everywhere it is written down, and
it is written down three times: here in the domain, as a `CHECK` on the model,
and as SQL in the migration that created the column. These pin them.
"""

from __future__ import annotations

import pytest
from sqlalchemy import CheckConstraint, Table

from app.domain.interest import (
    FEATURE_PATTERN,
    MAX_FEATURES_PER_ACCOUNT,
    is_feature_key,
)
from app.infra.db.models.platform import FeatureInterest

#: The constraint's name in the database, as the migration created it.
#:
#: **Compared exactly, not with `endswith`.** The copy of this test written first
#: used `endswith`, which happily matched
#: `ck_feature_interest_ck_feature_interest_feature_is_a_slug` — the doubled name
#: the model rendered while passing an already-prefixed name through
#: `NAMING_CONVENTION`. A loose match in the helper is what let a defect hide
#: inside a passing pin.
CHECK_NAME = "ck_feature_interest_feature_is_a_slug"


def _check(table: Table, name: str) -> str:
    (constraint,) = [
        c for c in table.constraints if isinstance(c, CheckConstraint) and str(c.name) == name
    ]
    return str(constraint.sqltext)


def test_the_feature_check_is_the_one_pattern() -> None:
    """`domain.interest` owns the shape; the column restates it as a `CHECK`.

    **The claim that this test exists came before the test did.** Three
    docstrings asserted the copies were pinned while nothing compared them —
    found by a review, and a comment promising a guarantee it has not got is
    worse than no comment, because the next person widens the pattern and trusts
    it. Concretely: widen `FEATURE_PATTERN` to 60 characters, `is_feature_key`
    accepts the key, the insert violates the `CHECK`, and a Notify-me press
    becomes a `500`.
    """
    assert FEATURE_PATTERN in _check(FeatureInterest.__table__, CHECK_NAME)


@pytest.mark.parametrize(
    "key",
    [
        "payments",
        "new_mentors",
        "zoom",
        "ab",
        "a" * 40,
        "a1_b2",
    ],
)
def test_a_well_formed_key_is_accepted(key: str) -> None:
    """The two members in use today, plus the boundaries either side of them."""
    assert is_feature_key(key)


@pytest.mark.parametrize(
    ("key", "why"),
    [
        ("", "empty"),
        ("a", "one character — the pattern requires at least two"),
        ("a" * 41, "41 characters — one past the limit"),
        ("Payments", "an upper-case letter"),
        ("1payments", "starts with a digit"),
        ("_payments", "starts with an underscore"),
        ("pay-ments", "a hyphen, which is not in the class"),
        ("pay ments", "a space"),
        ("payments\n", "a trailing newline"),
        ("payments ", "a trailing space"),
        ("pay.ments", "a dot"),
        ("payménts", "a non-ASCII letter"),
    ],
)
def test_a_malformed_key_is_refused(key: str, why: str) -> None:
    """**`fullmatch`, not `match`**, which is what the trailing cases prove.

    `re.match` anchors only the start, so `"payments\\n"` and `"payments "` would
    both pass a `match` against this pattern and reach the column — where the
    `CHECK` refuses them, turning a client typo into a `500` instead of a `422`.
    The newline case is the one a `$`-anchored pattern also lets through, because
    `$` matches before a final newline.
    """
    assert not is_feature_key(key), why


def test_the_per_account_cap_is_far_above_any_honest_use() -> None:
    """A bound on rows, not a product rule.

    `UNIQUE (user_id, feature)` means a repeated press adds nothing, so this is
    the only thing between an open vocabulary and an account looping fresh keys.
    Asserted as a floor rather than an exact number: the point is that no real
    person meets it, and pinning the exact value would make a deliberate change
    look like a break.
    """
    assert MAX_FEATURES_PER_ACCOUNT >= 20
