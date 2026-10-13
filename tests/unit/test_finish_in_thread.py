"""`_finish_in_thread` keeps its caller waiting until the thread is done (#362).

The image clean-up holds its advisory lock until its transaction ends, and the
transaction ends when the caller unwinds. So the caller must not unwind while
the storage delete is still running on its thread, however many times it is
cancelled; otherwise an identical upload can take the lock and commit before the
delete lands (#320, #359).
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from app.infra.db.asset_store import _finish_in_thread

pytestmark = pytest.mark.asyncio


class Blocking:
    """A storage call that runs until released, and records that it finished."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def __call__(self, _url: str) -> bool:
        self.started.set()
        # Capped, so a broken test fails rather than hangs.
        self.release.wait(timeout=10)
        self.finished.set()
        return True


async def started(call: Blocking) -> None:
    assert await asyncio.to_thread(call.started.wait, 5)


@pytest.mark.parametrize("cancels", [1, 2, 3])
async def test_the_caller_waits_out_the_thread_however_often_cancelled(cancels: int) -> None:
    call = Blocking()
    task = asyncio.create_task(_finish_in_thread(call, "url"))
    await started(call)

    for _ in range(cancels):
        task.cancel()
        await asyncio.sleep(0.05)

    # Still waiting: the thread has not finished, so the caller must not have.
    assert not task.done()
    call.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert call.finished.is_set()


async def test_uncancelled_it_returns_the_threads_answer() -> None:
    call = Blocking()
    call.release.set()

    assert await _finish_in_thread(call, "url") is True
    assert call.finished.is_set()
