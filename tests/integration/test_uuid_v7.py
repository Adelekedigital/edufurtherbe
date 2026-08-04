"""``uuid_generate_v7()`` conformance.

Every primary key in this database will come from this function, and it is
hand-written bit manipulation, so it is asserted against RFC 9562 rather than
against whatever it happens to emit.

Parsing is done with Python's ``uuid`` module rather than SQL bit arithmetic: the
stdlib is an independent implementation of the same spec, so a shared
misunderstanding between the function and its test is much less likely.
"""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

MS_PER_SECOND = 1000
# Generous: the assertion is "the prefix is the current time", not "the clocks
# agree to the millisecond". A wrong implementation is out by decades, not 5s.
MAX_CLOCK_DRIFT_MS = 5_000


def timestamp_ms(value: uuid.UUID) -> int:
    """The 48-bit big-endian millisecond timestamp UUIDv7 carries in its prefix."""
    return int(value.hex[:12], 16)


async def generate(engine: AsyncEngine, count: int = 1) -> list[uuid.UUID]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT uuid_generate_v7()::text AS value FROM generate_series(1, :n)"),
            {"n": count},
        )
        return [uuid.UUID(row.value) for row in result]


async def test_version_and_variant_match_the_spec(db_engine: AsyncEngine) -> None:
    (value,) = await generate(db_engine)

    assert value.version == 7
    assert value.variant == uuid.RFC_4122


async def test_timestamp_prefix_is_the_current_time(db_engine: AsyncEngine) -> None:
    """Catches the whole class of byte-order and offset mistakes at once.

    A prefix built from the wrong bytes does not land near now; it lands in 1970
    or in the year 50000.
    """
    (value,) = await generate(db_engine)

    now_ms = int(datetime.now(UTC).timestamp() * MS_PER_SECOND)

    assert abs(timestamp_ms(value) - now_ms) < MAX_CLOCK_DRIFT_MS


async def test_timestamp_prefixes_never_go_backwards(db_engine: AsyncEngine) -> None:
    """The property that actually buys index locality.

    Note what is NOT asserted: that the values themselves sort in generation
    order. UUIDv7 without RFC 9562's optional monotonic counter has no ordering
    guarantee *within* a millisecond, and 1,000 rows generate inside one — around
    half of consecutive pairs come out inverted. That is conformant behaviour,
    not a defect.

    The consequence is load-bearing and lives in the migration's docstring too:
    a UUIDv7 is not a safe sort key for cursor pagination. Order by a timestamp
    column with the id as a tiebreaker.
    """
    values = await generate(db_engine, count=1_000)
    prefixes = [timestamp_ms(value) for value in values]

    assert prefixes == sorted(prefixes)


async def test_values_from_different_milliseconds_are_ordered(db_engine: AsyncEngine) -> None:
    """The ordering guarantee that does hold, and the one callers may rely on."""
    (earlier,) = await generate(db_engine)
    await asyncio.sleep(0.01)
    (later,) = await generate(db_engine)

    assert timestamp_ms(later) > timestamp_ms(earlier)
    assert later > earlier


async def test_values_are_unique(db_engine: AsyncEngine) -> None:
    """A collision here is a silent primary-key conflict at insert time."""
    values = await generate(db_engine, count=1_000)

    assert len(set(values)) == len(values)
