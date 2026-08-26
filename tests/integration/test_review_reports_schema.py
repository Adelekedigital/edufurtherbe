"""What `review_reports` guarantees, and what no gate can see.

`alembic check` reads tables, columns, types and regular indexes. Everything
here is outside that set: two closed vocabularies, a uniqueness rule scoped to
a pair, an all-or-nothing resolution CHECK, a partial index, and a composite
foreign key that decides *who may report at all*.

Every constraint gets a rejecting **and** an accepting case. The one this file
exists for is the composite key: **only the subject of a review may report it.**
A single-column key on `reviews.id` is satisfied by any review, including
somebody else's — and every rejecting test below still passes when it is wrong.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

LAGOS = "Africa/Lagos"
RESOLVED = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)

INSERT_REPORT = """
INSERT INTO review_reports
    (review_id, reported_by, reason, detail, resolved_at, outcome, resolved_by)
VALUES (:review, :by, :reason, :detail, :resolved_at, :outcome, :resolved_by)
RETURNING id
"""


async def make_user(conn: AsyncConnection, email: str, role: str = "mentee") -> str:
    return str(
        (
            await conn.execute(
                text(
                    "INSERT INTO users (email, primary_role, timezone) "
                    "VALUES (:e, :r, :tz) RETURNING id"
                ),
                {"e": email, "r": role, "tz": LAGOS},
            )
        ).scalar_one()
    )


class Reports:
    """A mentor, a mentee, and a review of the mentor by the mentee."""

    def __init__(self, conn: AsyncConnection, mentor: str, mentee: str, review: str) -> None:
        self.conn = conn
        self.mentor = mentor
        self.mentee = mentee
        self.review = review

    async def review_of(self, subject: str, author: str) -> str:
        return str(
            (
                await self.conn.execute(
                    text(
                        "INSERT INTO reviews "
                        "(reviewed_by, reviewed_for, communication_rating, knowledge_rating, "
                        " practicality_rating, support_rating, valuable_rating, "
                        " nps_recommend_score, public_review) "
                        "VALUES (:by, :for_, 3, 3, 3, 3, 5, 9, 'Fine.') RETURNING id"
                    ),
                    {"by": author, "for_": subject},
                )
            ).scalar_one()
        )

    async def report(
        self,
        *,
        review: str | None = None,
        by: str | None = None,
        reason: str = "factually_inaccurate",
        detail: str | None = None,
        resolved_at: datetime | None = None,
        outcome: str | None = None,
        resolved_by: str | None = None,
    ) -> str:
        result = await self.conn.execute(
            text(INSERT_REPORT),
            {
                "review": review or self.review,
                "by": by or self.mentor,
                "reason": reason,
                "detail": detail,
                "resolved_at": resolved_at,
                "outcome": outcome,
                "resolved_by": resolved_by,
            },
        )
        return str(result.scalar_one())


@pytest_asyncio.fixture
async def reports(db_engine: AsyncEngine) -> AsyncIterator[Reports]:
    async with db_engine.begin() as conn:
        mentor = await make_user(conn, "mentor@example.test", role="mentor")
        mentee = await make_user(conn, "mentee@example.test")
        world = Reports(conn, mentor, mentee, "")
        world.review = await world.review_of(mentor, mentee)
        yield world


# --------------------------------------------------------------------------
# Who may report — the reason this file exists
# --------------------------------------------------------------------------


async def test_the_subject_may_report_a_review_of_themselves(reports: Reports) -> None:
    assert await reports.report()


async def test_nobody_else_may_report_it(reports: Reports) -> None:
    """**The guard no single-column key can give.**

    On `reviews.id` alone this row inserts happily: a stranger — or the review's
    own author — could file a report against a review that has nothing to do
    with them, and the admin queue would carry it. `(review_id, reported_by)`
    against `(id, reviewed_for)` makes that unrepresentable.
    """
    stranger = await make_user(reports.conn, "stranger@example.test")

    with pytest.raises(IntegrityError, match="report_belongs_to_subject"):
        await reports.report(by=stranger)


async def test_the_author_may_not_report_their_own_review(reports: Reports) -> None:
    """The same key catches this for free. An author who regrets a review
    withdraws it; reporting is the *subject's* channel, and conflating them
    would let the author route their own text through moderation."""
    with pytest.raises(IntegrityError, match="report_belongs_to_subject"):
        await reports.report(by=reports.mentee)


async def test_a_subject_may_report_each_review_about_them(reports: Reports) -> None:
    """The accepting half: the key scopes to the *pair*, so a mentor with two
    reviews may report both."""
    another = await make_user(reports.conn, "second-mentee@example.test")
    second = await reports.review_of(reports.mentor, another)

    assert await reports.report()
    assert await reports.report(review=second)


async def test_one_report_per_person_per_review(reports: Reports) -> None:
    """Reporting the same review twice is a duplicate, not a second complaint —
    and a queue carrying both would be adjudicated twice."""
    await reports.report()

    with pytest.raises(IntegrityError, match="review_id"):
        await reports.report(reason="abusive")


# --------------------------------------------------------------------------
# The vocabularies
# --------------------------------------------------------------------------


async def test_the_reason_vocabulary_is_closed(reports: Reports) -> None:
    with pytest.raises(IntegrityError, match="reason_is_known"):
        await reports.report(reason="i_dislike_it")


async def test_every_shipped_reason_is_accepted(reports: Reports) -> None:
    """The accepting half. A CHECK that refuses everything also refuses garbage."""
    for n, reason in enumerate(("factually_inaccurate", "abusive", "not_this_session", "spam")):
        author = await make_user(reports.conn, f"author-{n}@example.test")
        review = await reports.review_of(reports.mentor, author)

        assert await reports.report(review=review, reason=reason)


async def test_the_outcome_vocabulary_is_closed(reports: Reports) -> None:
    admin = await make_user(reports.conn, "admin@example.test")

    with pytest.raises(IntegrityError, match="outcome_is_known"):
        await reports.report(resolved_at=RESOLVED, outcome="ignored", resolved_by=admin)


# --------------------------------------------------------------------------
# Resolution is all three columns or none
# --------------------------------------------------------------------------


async def test_a_fresh_report_is_unresolved(reports: Reports) -> None:
    assert await reports.report()


async def test_a_resolved_report_carries_all_three(reports: Reports) -> None:
    admin = await make_user(reports.conn, "admin@example.test")

    assert await reports.report(resolved_at=RESOLVED, outcome="upheld", resolved_by=admin)


async def test_an_outcome_without_a_moment_is_refused(reports: Reports) -> None:
    """**Half-resolved is the state that reads as a bug in the queue.** A report
    with an outcome and no timestamp is neither pending nor closed, and every
    filter over the queue has to guess which."""
    with pytest.raises(IntegrityError, match="resolution_is_whole"):
        await reports.report(outcome="upheld")


async def test_a_moment_without_an_outcome_is_refused(reports: Reports) -> None:
    with pytest.raises(IntegrityError, match="resolution_is_whole"):
        await reports.report(resolved_at=RESOLVED)


async def test_a_resolution_without_an_admin_is_refused(reports: Reports) -> None:
    """Somebody decided this. `resolved_by` is who, and a moderation record
    that cannot say is not a record."""
    with pytest.raises(IntegrityError, match="resolution_is_whole"):
        await reports.report(resolved_at=RESOLVED, outcome="upheld")


# --------------------------------------------------------------------------
# Deletion policy — a report is moderation evidence
# --------------------------------------------------------------------------


async def test_a_reported_review_cannot_be_hard_deleted(reports: Reports) -> None:
    """ADR 0013: restrict where the child is evidence. Upholding a report
    soft-deletes the review; the report itself must outlive that, or the record
    of *why* a review disappeared goes with it."""
    await reports.report()

    with pytest.raises(IntegrityError, match="review_reports"):
        await reports.conn.execute(text("DELETE FROM reviews WHERE id = :r"), {"r": reports.review})


async def test_a_reporter_cannot_be_hard_deleted(reports: Reports) -> None:
    """**Asserted on the outcome, not the mechanism — and that is forced.**

    The composite key means a reporter is always the review's `reviewed_for`, so
    `fk_reviews_reviewed_for_users` blocks the delete before `review_reports` is
    ever consulted. Naming `review_reports` here would be a test reporting on a
    constraint it never reached — the shape that has already bitten this
    codebase twice, on the credit ledger and on referrals.

    So this asserts that the reporter survives, which is the property that
    matters, and names the key that actually enforces it.
    """
    await reports.report()

    with pytest.raises(IntegrityError, match="reviewed_for"):
        await reports.conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": reports.mentor})


async def test_a_resolving_admin_cannot_be_hard_deleted(reports: Reports) -> None:
    """**This one does reach `review_reports`**, which is why it is here.

    An admin who adjudicated is named by nothing else in the row, so
    `fk_review_reports_resolved_by_users` is the constraint that fires — and a
    moderation record whose decider could be deleted could no longer say who
    decided.
    """
    admin = await make_user(reports.conn, "admin@example.test")
    await reports.report(resolved_at=RESOLVED, outcome="upheld", resolved_by=admin)

    with pytest.raises(IntegrityError, match="review_reports"):
        await reports.conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": admin})


async def test_a_report_cannot_name_a_review_that_does_not_exist(reports: Reports) -> None:
    with pytest.raises(IntegrityError, match="report_belongs_to_subject"):
        await reports.report(review=str(uuid.uuid4()))


# --------------------------------------------------------------------------
# The table is mutable, so it maintains updated_at
# --------------------------------------------------------------------------


async def test_the_table_maintains_updated_at(reports: Reports) -> None:
    """Not append-only: a report is resolved in place, which is an update
    rather than a second row. A second row would make "is this pending" an
    aggregate instead of a lookup."""
    result = await reports.conn.execute(
        text(
            "SELECT t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE c.relname = 'review_reports' AND NOT t.tgisinternal"
        )
    )

    assert [row[0] for row in result] == ["trg_set_updated_at"]
