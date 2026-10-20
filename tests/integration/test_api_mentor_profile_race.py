"""Mentor-profile PATCHes arriving together each land whole.

`update_mentor_profile` takes `lock_mentor` first, so two replacements of one
mentor's help areas run one after the other. Without it both delete, both
insert, and the later insert meets the earlier one's rows on
`mentor_service_offerings`' unique pair and answers 500. The guide-country
list has its own race test beside its other tests.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from tests.integration.factories import make_public_mentor, token_for
from tests.integration.test_api_writes import url

from conftest import bearer

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


async def test_offering_replacements_arriving_together_each_land_whole(
    api_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    mentor = await make_public_mentor(db_engine, "offering-race")
    headers = bearer(await token_for(db_engine, mentor))
    async with db_engine.connect() as conn:
        offerings = [
            str(row.id)
            for row in await conn.execute(
                text("SELECT id FROM service_offerings WHERE is_active ORDER BY sort_order")
            )
        ]
    # Overlapping lists, so each insert collides with another's rows.
    lists = [offerings[i : i + 3] for i in range(len(offerings) - 2)]
    assert len(lists) >= 2

    answers = await asyncio.gather(
        *(
            api_client.patch(
                url(mentor, "mentor-profile"), headers=headers, json={"offering_ids": ids}
            )
            for ids in lists
        )
    )

    assert [a.status_code for a in answers] == [200] * len(lists), [a.text for a in answers]
    async with db_engine.connect() as conn:
        stored = {
            str(row.service_offering_id)
            for row in await conn.execute(
                text(
                    "SELECT service_offering_id FROM mentor_service_offerings "
                    "WHERE mentor_user_id = :u"
                ),
                {"u": mentor},
            )
        }
    assert stored in [set(ids) for ids in lists]
