"""A mentor's profile link figures over windows of UTC days (decision 353).

The read side of decision 346's two tables, **parameterised by mentor and days
and nothing else**, so a mentor's own view, an admin's view of one mentor, an
export or a share card all read the same counts. Whoever calls it has already
decided the caller may see ``mentor``'s figures; this module only counts.

**Counts only.** No visitor key, day-by-day row or anything else about one
visitor leaves the statement: a row is one visitor's day, so the visit count
*is* the count of distinct visitors per day, and nothing is counted across days
(decision 348 deletes the key that could join them).

**Scoped in the statement** (non-negotiable #5): every part names the mentor.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Date,
    Integer,
    Text,
    and_,
    cast,
    column,
    func,
    literal,
    select,
    union_all,
    values,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import LinkVia, ShareMethod
from app.domain.link_tracking import TOP_REFERRERS, DaySpan
from app.infra.db.models.platform import MentorProfileShare, MentorProfileVisit
from app.infra.db.predicates import during

__all__ = ["link_stats"]

_VIA, _REFERRER, _METHOD = "via", "referrer", "method"


async def link_stats(
    session: AsyncSession, mentor: UUID, spans: Sequence[DaySpan]
) -> list[dict[str, Any]]:
    """``mentor``'s visits and shares in each of ``spans``, in their order.

    Each block: its `since`/`until`, `visits` and `visits_by_via` (every
    `LinkVia` member then ``None``, zeros included), `top_referrers` (at most
    `TOP_REFERRERS` hosts, most visits first, a tie by host), and `shares` and
    `shares_by_method` (every `ShareMethod`), the mentor's own shares left out.

    **One statement for every span**, so they read one snapshot.
    """
    if not spans:
        return []
    rows = (await session.execute(_statement(mentor, spans))).all()
    blocks = [_empty(span) for span in spans]
    for kind, position, key, count in rows:
        block = blocks[position]
        if kind == _VIA:
            block["via"][None if key is None else LinkVia(key)] = count
        elif kind == _METHOD:
            block["method"][ShareMethod(key)] = count
        else:
            block["referrers"].append((key, count))
    return [_shaped(block) for block in blocks]


def _empty(span: DaySpan) -> dict[str, Any]:
    via: dict[LinkVia | None, int] = dict.fromkeys([*LinkVia, None], 0)
    method: dict[ShareMethod, int] = dict.fromkeys(ShareMethod, 0)
    return {"span": span, "via": via, "method": method, "referrers": []}


def _shaped(block: dict[str, Any]) -> dict[str, Any]:
    span: DaySpan = block["span"]
    referrers = sorted(block["referrers"], key=lambda pair: (-pair[1], pair[0]))
    return {
        "since": span.since,
        "until": span.until,
        "visits": sum(block["via"].values()),
        "visits_by_via": [{"key": key, "count": n} for key, n in block["via"].items()],
        "top_referrers": [{"host": host, "count": n} for host, n in referrers],
        "shares": sum(block["method"].values()),
        "shares_by_method": [{"key": key, "count": n} for key, n in block["method"].items()],
    }


def _statement(mentor: UUID, spans: Sequence[DaySpan]) -> Any:
    """`(kind, position, key, count)` rows: a `UNION ALL` of visits by channel,
    the top referrers and shares by method, each grouped by span.

    The spans are a `VALUES` list, as the monthly chart's months are, joined by
    `during()`. Each part also bounds `day` by the outermost edges as
    constants, so the scan is a range on the unique key's leading
    `(mentor_user_id, day)` rather than every row the mentor has.
    """
    bounds = (
        values(
            column("position", Integer),
            column("since", Date),
            column("until", Date),
            name="spans",
        )
        .data([(position, s.since, s.until) for position, s in enumerate(spans)])
        .alias("spans")
    )
    span = bounds.c
    first, end = min(s.since for s in spans), max(s.until for s in spans)

    def scoped(table: Any) -> Any:
        return and_(table.mentor_user_id == mentor, table.day >= first, table.day < end)

    visit, shared = MentorProfileVisit, MentorProfileShare
    by_via = (
        select(
            literal(_VIA).label("kind"),
            span.position,
            cast(visit.via, Text).label("key"),
            func.count().label("count"),
        )
        .select_from(visit)
        .join(bounds, during(visit.day, span))
        .where(scoped(visit))
        .group_by(span.position, visit.via)
    )
    ranked = (
        select(
            span.position,
            visit.referrer_host.label("key"),
            func.count().label("count"),
            func.row_number()
            .over(
                partition_by=span.position,
                order_by=(func.count().desc(), visit.referrer_host),
            )
            .label("rank"),
        )
        .select_from(visit)
        .join(bounds, during(visit.day, span))
        .where(scoped(visit), visit.referrer_host.is_not(None))
        .group_by(span.position, visit.referrer_host)
        .subquery("ranked")
    )
    top = select(
        literal(_REFERRER).label("kind"), ranked.c.position, ranked.c.key, ranked.c.count
    ).where(ranked.c.rank <= TOP_REFERRERS)
    by_method = (
        select(
            literal(_METHOD).label("kind"),
            span.position,
            cast(shared.method, Text).label("key"),
            func.count().label("count"),
        )
        .select_from(shared)
        .join(bounds, during(shared.day, span))
        .where(scoped(shared), shared.own.is_(False))
        .group_by(span.position, shared.method)
    )
    return union_all(by_via, top, by_method)
