"""The moderation queue: every review, and deciding a report.

**The assertion this file exists for is what upholding does to the profile.**
The report table adds a reason and a record; the *removal* is
`reviews.deleted_at`, which the profile's partial index already honours. So the
test that matters follows an upheld report all the way out to the public
endpoints and checks the review has left both the list and the average.
"""

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

QUEUE = "/api/v1/admin/reviews"


async def a_user(
    engine: AsyncEngine, auth_id: UUID, *, role: str = "mentee", admin: str | None = None
) -> UUID:
    async with engine.begin() as conn:
        user_id = (
            await conn.execute(
                text(
                    "INSERT INTO users (email, auth_id, first_name, last_name, "
                    "primary_role, timezone) VALUES (:e, :a, 'Ada', 'Lovelace', :r, "
                    "'Africa/Lagos') RETURNING id"
                ),
                {"e": f"{auth_id}@example.com", "a": auth_id, "r": role},
            )
        ).scalar_one()
        if admin is not None:
            await conn.execute(
                text("INSERT INTO admin_users (user_id, admin_role) VALUES (:u, :r)"),
                {"u": user_id, "r": admin},
            )
        return UUID(str(user_id))


async def a_review(engine: AsyncEngine, *, about: UUID, by: UUID, value: int = 5) -> UUID:
    async with engine.begin() as conn:
        return UUID(
            str(
                (
                    await conn.execute(
                        text(
                            "INSERT INTO reviews (reviewed_by, reviewed_for, "
                            "communication_rating, knowledge_rating, practicality_rating, "
                            "support_rating, valuable_rating, nps_recommend_score, "
                            "public_review) VALUES (:by, :for_, 3, 3, 3, 3, :v, 9, 'Fine.') "
                            "RETURNING id"
                        ),
                        {"by": by, "for_": about, "v": value},
                    )
                ).scalar_one()
            )
        )


async def a_report(client: httpx.AsyncClient, subject: UUID, review: UUID) -> UUID:
    response = await client.post(
        f"/api/v1/me/reviews/{review}/report",
        json={"reason": "abusive"},
        headers=bearer(api_token(subject)),
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


async def decide(
    client: httpx.AsyncClient, admin: UUID, report: UUID, outcome: str
) -> httpx.Response:
    return await client.post(
        f"/api/v1/admin/review-reports/{report}/decision",
        json={"outcome": outcome},
        headers=bearer(api_token(admin)),
    )


async def queue(client: httpx.AsyncClient, admin: UUID, **params: Any) -> Any:
    body = (await client.get(QUEUE, params=params, headers=bearer(api_token(admin)))).json()
    return body["data"]


# --------------------------------------------------------------------------
# Who may look
# --------------------------------------------------------------------------


async def test_a_non_admin_gets_a_404(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """404, not 403 — the same rule `require_admin` follows everywhere: a 403
    tells the caller the endpoint is real."""
    nobody = uuid4()
    await a_user(db_engine, nobody)

    response = await api_client.get(QUEUE, headers=bearer(api_token(nobody)))

    assert response.status_code == 404


async def test_no_token_is_refused(api_client: httpx.AsyncClient) -> None:
    assert (await api_client.get(QUEUE)).status_code == 401


# --------------------------------------------------------------------------
# The queue shows everything, reported or not
# --------------------------------------------------------------------------


async def test_the_queue_lists_every_review(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**All reviews, not only the reported ones.** A moderator asked to judge a
    complaint needs the surrounding record — one review in isolation says
    nothing about whether an author is a pattern."""
    admin, subject, author = uuid4(), uuid4(), uuid4()
    await a_user(db_engine, admin, admin="super_admin")
    subject_id = await a_user(db_engine, subject, role="mentor")
    author_id = await a_user(db_engine, author)
    await a_review(db_engine, about=subject_id, by=author_id)
    await a_review(db_engine, about=subject_id, by=author_id)

    assert len(await queue(api_client, admin)) == 2


async def test_the_queue_can_be_narrowed_to_reported(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    admin, subject, author = uuid4(), uuid4(), uuid4()
    await a_user(db_engine, admin, admin="super_admin")
    subject_id = await a_user(db_engine, subject, role="mentor")
    author_id = await a_user(db_engine, author)
    reported = await a_review(db_engine, about=subject_id, by=author_id)
    await a_review(db_engine, about=subject_id, by=author_id)
    await a_report(api_client, subject, reported)

    rows = await queue(api_client, admin, reported="true")

    assert [UUID(row["id"]) for row in rows] == [reported]


async def test_a_queued_row_carries_its_report(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    admin, subject, author = uuid4(), uuid4(), uuid4()
    await a_user(db_engine, admin, admin="super_admin")
    subject_id = await a_user(db_engine, subject, role="mentor")
    author_id = await a_user(db_engine, author)
    review = await a_review(db_engine, about=subject_id, by=author_id)
    await a_report(api_client, subject, review)

    row = (await queue(api_client, admin, reported="true"))[0]

    assert row["report"]["reason"] == "abusive"
    assert row["report"]["outcome"] is None


# --------------------------------------------------------------------------
# Deciding — and what upholding actually does
# --------------------------------------------------------------------------


async def test_upholding_removes_the_review_from_the_profile(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**The assertion this file exists for.**

    Followed through the reads that actually serve the profile rather than
    checked against `deleted_at` — the column moving only counts if
    `published()` honours it, and asserting the column alone would pass even if
    the list and the average did not change.

    `published()` is character-for-character the predicate of
    `ix_reviews_mentor_valuable`, so this is also what catches the two drifting.
    """
    from app.infra.db.engine import create_session_factory
    from app.infra.db.review_reader import list_mentor_reviews
    from app.infra.db.review_stats import mentor_review_stats

    admin, subject, author = uuid4(), uuid4(), uuid4()
    await a_user(db_engine, admin, admin="super_admin")
    subject_id = await a_user(db_engine, subject, role="mentor")
    author_id = await a_user(db_engine, author)
    review = await a_review(db_engine, about=subject_id, by=author_id)
    report = await a_report(api_client, subject, review)

    factory = create_session_factory(db_engine)
    async with factory() as db:
        before_rows, _ = await list_mentor_reviews(db, subject_id, limit=10)
        before_stats = await mentor_review_stats(db, subject_id)

    assert [row["id"] for row in before_rows] == [review]
    assert before_stats["review_count"] == 1

    assert (await decide(api_client, admin, report, "upheld")).status_code == 200

    async with factory() as db:
        after_rows, _ = await list_mentor_reviews(db, subject_id, limit=10)
        after_stats = await mentor_review_stats(db, subject_id)

    assert after_rows == []
    assert after_stats["review_count"] == 0


async def test_dismissing_leaves_the_review_alone(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """The other half, and the one that keeps reporting honest: a complaint that
    is not upheld changes nothing about the review."""
    admin, subject, author = uuid4(), uuid4(), uuid4()
    await a_user(db_engine, admin, admin="super_admin")
    subject_id = await a_user(db_engine, subject, role="mentor")
    author_id = await a_user(db_engine, author)
    review = await a_review(db_engine, about=subject_id, by=author_id)
    report = await a_report(api_client, subject, review)

    assert (await decide(api_client, admin, report, "dismissed")).status_code == 200

    async with db_engine.begin() as conn:
        deleted = (
            await conn.execute(text("SELECT deleted_at FROM reviews WHERE id = :r"), {"r": review})
        ).scalar_one()

    assert deleted is None


async def test_a_decision_records_who_made_it(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """All three resolution columns move together — the CHECK requires it, and
    a moderation record that cannot say who decided is not a record."""
    admin, subject, author = uuid4(), uuid4(), uuid4()
    admin_id = await a_user(db_engine, admin, admin="super_admin")
    subject_id = await a_user(db_engine, subject, role="mentor")
    author_id = await a_user(db_engine, author)
    review = await a_review(db_engine, about=subject_id, by=author_id)
    report = await a_report(api_client, subject, review)

    await decide(api_client, admin, report, "upheld")

    async with db_engine.begin() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT resolved_by, outcome, resolved_at IS NOT NULL AS done "
                        "FROM review_reports WHERE id = :r"
                    ),
                    {"r": report},
                )
            )
            .mappings()
            .one()
        )

    assert row["resolved_by"] == admin_id
    assert row["outcome"] == "upheld"
    assert row["done"] is True


async def test_deciding_twice_is_refused(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**A second decision would rewrite the first**, and the record of who
    decided what would be whatever the last admin clicked."""
    admin, subject, author = uuid4(), uuid4(), uuid4()
    await a_user(db_engine, admin, admin="super_admin")
    subject_id = await a_user(db_engine, subject, role="mentor")
    author_id = await a_user(db_engine, author)
    review = await a_review(db_engine, about=subject_id, by=author_id)
    report = await a_report(api_client, subject, review)
    await decide(api_client, admin, report, "dismissed")

    assert (await decide(api_client, admin, report, "upheld")).status_code == 409


async def test_an_unknown_report_is_a_404(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    admin = uuid4()
    await a_user(db_engine, admin, admin="super_admin")

    assert (await decide(api_client, admin, uuid4(), "upheld")).status_code == 404


async def test_a_non_admin_cannot_decide(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """The subject filed it; they still do not get to rule on it. That split is
    the whole design."""
    subject, author = uuid4(), uuid4()
    subject_id = await a_user(db_engine, subject, role="mentor")
    author_id = await a_user(db_engine, author)
    review = await a_review(db_engine, about=subject_id, by=author_id)
    report = await a_report(api_client, subject, review)

    assert (await decide(api_client, subject, report, "upheld")).status_code == 404
