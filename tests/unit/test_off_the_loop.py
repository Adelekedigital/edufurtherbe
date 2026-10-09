"""The guard the suites enforce #370 with: it must refuse, and only refuse, a
call made on the event loop. A guard that never fires passes every suite."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from tests.off_the_loop import BlockedTheLoopError, guarded


@dataclass
class Adapter:
    calls: list[str] = field(default_factory=list)

    def send(self) -> str:
        self.calls.append("send")
        return "sent"

    async def busy(self) -> str:
        return "busy"


@pytest.mark.asyncio
async def test_a_method_called_on_the_loop_is_refused() -> None:
    adapter = guarded("notifier", Adapter())

    with pytest.raises(BlockedTheLoopError, match=r"notifier\.send"):
        adapter.send()


@pytest.mark.asyncio
async def test_a_method_called_from_a_thread_runs() -> None:
    adapter = Adapter()

    answer = await asyncio.to_thread(guarded("notifier", adapter).send)

    assert answer == "sent"
    assert adapter.calls == ["send"]


@pytest.mark.asyncio
async def test_a_bare_function_is_guarded_too() -> None:
    def exchange(**_: Any) -> str:
        return "tokens"

    with pytest.raises(BlockedTheLoopError):
        guarded("calendar_exchange", exchange)(code="c")
    assert await asyncio.to_thread(guarded("calendar_exchange", exchange), code="c") == "tokens"


@pytest.mark.asyncio
async def test_an_async_method_is_left_alone() -> None:
    """Awaiting one is on the loop by definition; it does its own thread hop."""
    assert await guarded("free_busy", Adapter()).busy() == "busy"


@pytest.mark.asyncio
async def test_what_a_fake_recorded_reads_through() -> None:
    adapter = Adapter()
    await asyncio.to_thread(guarded("notifier", adapter).send)

    assert guarded("notifier", adapter).calls == ["send"]


def test_outside_any_loop_nothing_is_refused() -> None:
    """Scripts and jobs run sync code with no loop; that is not blocking one."""
    assert guarded("notifier", Adapter()).send() == "sent"
