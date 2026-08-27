"""Reviews about you, and reporting one.

**The two that matter are the refusals.** Reporting is a request for
adjudication, so the tests that carry weight are the ones proving a subject
cannot reach somebody else's review — and that filing changes nothing on the
public profile, which is what separates reporting from hiding.
"""

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from conftest import api_token, bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

MINE = "/api/v1/me/reviews"


async def a_user(engine: AsyncEngine, auth_id: UUID, *, role: str = "mentee") -> UUID:
    async with engine.begin() as conn:
        return UUID(
            str(
                (
                    await conn.execute(
                        text(
                            "INSERT INTO users (email, auth_id, first_name, last_name, "
                            "primary_role, timezone) VALUES (:e, :a, 'Ada', 'Lovelace', "
                            ":r, 'Africa/Lagos') RETURNING id"
                        ),
                        {"e": f"{auth_id}@example.com", "a": auth_id, "r": role},
                    )
                ).scalar_one()
            )
        )


async def a_review(engine: AsyncEngine, *, about: UUID, by: UUID, text_: str = "Fine.") -> UUID:
    async with engine.begin() as conn:
        return UUID(
            str(
                (
                    await conn.execute(
                        text(
                            "INSERT INTO reviews (reviewed_by, reviewed_for, "
                            "communication_rating, knowledge_rating, practicality_rating, "
                            "support_rating, valuable_rating, nps_recommend_score, "
                            "public_review) VALUES (:by, :for_, 3, 3, 3, 3, 5, 9, :t) "
                            "RETURNING id"
                        ),
                        {"by": by, "for_": about, "t": text_},
                    )
                ).scalar_one()
            )
        )


async def report(
    client: httpx.AsyncClient, auth_id: UUID, review: UUID, reason: str = "abusive"
) -> httpx.Response:
    return await client.post(
        f"{MINE}/{review}/report",
        json={"reason": reason},
        headers=bearer(api_token(auth_id)),
    )


async def listed(client: httpx.AsyncClient, auth_id: UUID) -> Any:
    body = (await client.get(MINE, headers=bearer(api_token(auth_id)))).json()
    return body["data"]


# --------------------------------------------------------------------------
# Reporting — the refusals first
# --------------------------------------------------------------------------


async def test_a_review_about_somebody_else_is_a_404(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**404, not 403.** Confirming the review exists would turn an
    authorization answer into a way to enumerate reviews — the same rule
    `require_admin` follows for a non-admin."""
    subject, author, stranger = uuid4(), uuid4(), uuid4()
    subject_id = await a_user(db_engine, subject, role="mentor")
    author_id = await a_user(db_engine, author)
    await a_user(db_engine, stranger)
    review = await a_review(db_engine, about=subject_id, by=author_id)

    assert (await report(api_client, stranger, review)).status_code == 404


async def test_the_author_cannot_report_their_own_review(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """An author who regrets a review withdraws it. Reporting is the subject's
    channel, and conflating them would let an author route their own text
    through moderation."""
    subject, author = uuid4(), uuid4()
    subject_id = await a_user(db_engine, subject, role="mentor")
    author_id = await a_user(db_engine, author)
    review = await a_review(db_engine, about=subject_id, by=author_id)

    assert (await report(api_client, author, review)).status_code == 404


async def test_an_unknown_review_is_a_404(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    subject = uuid4()
    await a_user(db_engine, subject, role="mentor")

    assert (await report(api_client, subject, uuid4())).status_code == 404


async def test_reporting_twice_is_refused(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """A duplicate, not a second complaint — and a queue carrying both is
    adjudicated twice."""
    subject, author = uuid4(), uuid4()
    subject_id = await a_user(db_engine, subject, role="mentor")
    author_id = await a_user(db_engine, author)
    review = await a_review(db_engine, about=subject_id, by=author_id)
    await report(api_client, subject, review)

    assert (await report(api_client, subject, review, reason="spam")).status_code == 409


# --------------------------------------------------------------------------
# Reporting — the accepting case, and what it must not do
# --------------------------------------------------------------------------


async def test_the_subject_can_report_a_review_of_themselves(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    subject, author = uuid4(), uuid4()
    subject_id = await a_user(db_engine, subject, role="mentor")
    author_id = await a_user(db_engine, author)
    review = await a_review(db_engine, about=subject_id, by=author_id)

    response = await report(api_client, subject, review, reason="factually_inaccurate")

    assert response.status_code == 201
    assert response.headers["Location"] == MINE
    body = response.json()
    assert body["reason"] == "factually_inaccurate"
    assert body["outcome"] is None
    assert body["resolved_at"] is None


async def test_reporting_does_not_hide_the_review(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**The assertion that separates reporting from hiding.**

    If filing removed the review, a mentor could clear their profile one
    complaint at a time and the rating would mean nothing. It stays until an
    admin upholds the report.
    """
    subject, author = uuid4(), uuid4()
    subject_id = await a_user(db_engine, subject, role="mentor")
    author_id = await a_user(db_engine, author)
    review = await a_review(db_engine, about=subject_id, by=author_id)

    await report(api_client, subject, review)

    async with db_engine.begin() as conn:
        deleted = (
            await conn.execute(text("SELECT deleted_at FROM reviews WHERE id = :r"), {"r": review})
        ).scalar_one()

    assert deleted is None


async def test_a_withdrawn_review_is_still_reportable(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """It may have been withdrawn by its author rather than by moderation, and a
    subject who wants the record examined should not be blocked by the author
    having second-guessed themselves first."""
    subject, author = uuid4(), uuid4()
    subject_id = await a_user(db_engine, subject, role="mentor")
    author_id = await a_user(db_engine, author)
    review = await a_review(db_engine, about=subject_id, by=author_id)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE reviews SET deleted_at = now() WHERE id = :r"), {"r": review}
        )

    assert (await report(api_client, subject, review)).status_code == 201


# --------------------------------------------------------------------------
# The subject's own list
# --------------------------------------------------------------------------


async def test_the_list_shows_only_reviews_about_you(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Object-level scoping, in the query rather than after it."""
    subject, other, author = uuid4(), uuid4(), uuid4()
    subject_id = await a_user(db_engine, subject, role="mentor")
    other_id = await a_user(db_engine, other, role="mentor")
    author_id = await a_user(db_engine, author)
    await a_review(db_engine, about=subject_id, by=author_id, text_="Yours.")
    await a_review(db_engine, about=other_id, by=author_id, text_="Theirs.")

    assert [row["public_review"] for row in await listed(api_client, subject)] == ["Yours."]


async def test_a_withdrawn_review_is_visible_to_its_subject(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """**What the public list cannot show.** Absence is the point of withdrawal
    there; here the subject needs to see that it went."""
    subject, author = uuid4(), uuid4()
    subject_id = await a_user(db_engine, subject, role="mentor")
    author_id = await a_user(db_engine, author)
    review = await a_review(db_engine, about=subject_id, by=author_id)
    async with db_engine.begin() as conn:
        await conn.execute(
            text("UPDATE reviews SET deleted_at = now() WHERE id = :r"), {"r": review}
        )

    rows = await listed(api_client, subject)

    assert [row["withdrawn"] for row in rows] == [True]


async def test_an_unreported_review_carries_no_report(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Null is *not reported*, which is a different state from
    reported-and-dismissed — and a client renders them differently."""
    subject, author = uuid4(), uuid4()
    subject_id = await a_user(db_engine, subject, role="mentor")
    author_id = await a_user(db_engine, author)
    await a_review(db_engine, about=subject_id, by=author_id)

    assert (await listed(api_client, subject))[0]["report"] is None


async def test_a_reported_review_carries_its_report(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    subject, author = uuid4(), uuid4()
    subject_id = await a_user(db_engine, subject, role="mentor")
    author_id = await a_user(db_engine, author)
    review = await a_review(db_engine, about=subject_id, by=author_id)
    await report(api_client, subject, review, reason="not_this_session")

    filed = (await listed(api_client, subject))[0]["report"]

    assert filed["reason"] == "not_this_session"
    assert filed["outcome"] is None


async def test_the_report_never_names_the_moderator(
    db_engine: AsyncEngine, api_client: httpx.AsyncClient
) -> None:
    """Naming the admin to the person they ruled against invites exactly the
    pressure moderation exists to absorb."""
    subject, author, admin = uuid4(), uuid4(), uuid4()
    subject_id = await a_user(db_engine, subject, role="mentor")
    author_id = await a_user(db_engine, author)
    admin_id = await a_user(db_engine, admin)
    review = await a_review(db_engine, about=subject_id, by=author_id)
    await report(api_client, subject, review)
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE review_reports SET resolved_at = now(), outcome = 'dismissed', "
                "resolved_by = :a WHERE review_id = :r"
            ),
            {"a": admin_id, "r": review},
        )

    filed = (await listed(api_client, subject))[0]["report"]

    assert filed["outcome"] == "dismissed"
    assert "resolved_by" not in filed


async def test_no_token_is_refused(api_client: httpx.AsyncClient) -> None:
    assert (await api_client.get(MINE)).status_code == 401
    assert (
        await api_client.post(f"{MINE}/{uuid4()}/report", json={"reason": "spam"})
    ).status_code == 401
