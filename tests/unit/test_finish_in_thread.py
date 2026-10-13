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
from typing import Any

import pytest

from app.infra.db import asset_store
from app.infra.db.asset_store import _finish_in_thread

pytestmark = pytest.mark.asyncio


class Blocking:
    """A storage call that runs until released, then answers or raises."""

    def __init__(self, error: Exception | None = None) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.error = error

    def __call__(self, _url: str) -> bool:
        self.started.set()
        # Capped, so a broken test fails rather than hangs.
        self.release.wait(timeout=10)
        self.finished.set()
        if self.error is not None:
            raise self.error
        return True


async def started(call: Blocking) -> None:
    assert await asyncio.to_thread(call.started.wait, 5)


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """How many waits the function began: one, plus one per cancel it received,
    so the count proves each cancel was delivered and survived."""
    wait = asyncio.wait
    begun: list[int] = []

    def counted(*args: Any, **kwargs: Any) -> Any:
        begun.append(1)
        return wait(*args, **kwargs)

    monkeypatch.setattr(asset_store.asyncio, "wait", counted)
    return begun


@pytest.mark.parametrize("cancels", [1, 2, 3])
async def test_the_caller_waits_out_the_thread_however_often_cancelled(
    cancels: int, waits: list[int]
) -> None:
    call = Blocking()
    task = asyncio.create_task(_finish_in_thread(call, "url"))
    await started(call)

    for delivered in range(1, cancels + 1):
        task.cancel(f"cancel {delivered}")
        for _ in range(100):
            if len(waits) == delivered + 1:
                break
            await asyncio.sleep(0.01)
        assert len(waits) == delivered + 1, "a cancel was not delivered on its own"

    # Still waiting: the thread has not finished, so the caller must not have.
    assert not task.done()
    call.release.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await task
    assert call.finished.is_set()
    # The first cancel is the one re-raised, with its message.
    assert raised.value.args == ("cancel 1",)


async def test_uncancelled_it_returns_the_threads_answer() -> None:
    call = Blocking()
    call.release.set()

    assert await _finish_in_thread(call, "url") is True
    assert call.finished.is_set()


async def test_uncancelled_a_failure_of_the_thread_is_raised() -> None:
    call = Blocking(error=RuntimeError("storage broke"))
    call.release.set()

    with pytest.raises(RuntimeError, match="storage broke"):
        await _finish_in_thread(call, "url")


async def test_cancelled_a_failure_of_the_thread_is_logged_not_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cancel is what propagates, so the thread's own failure is logged
    rather than dropped with nothing to show for the orphaned object.

    Read from the module's logger itself, not `caplog`: the app's logging setup
    (JSON, #425) can stop records reaching the root logger depending on which
    tests ran first, which made a `caplog` assertion order-dependent in CI."""
    logged: list[BaseException | None] = []
    monkeypatch.setattr(
        asset_store.logger,
        "warning",
        lambda *_args, exc_info=None, **_kwargs: logged.append(exc_info),
    )
    call = Blocking(error=RuntimeError("storage broke"))
    task = asyncio.create_task(_finish_in_thread(call, "url"))
    await started(call)
    task.cancel()
    await asyncio.sleep(0.05)
    call.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(logged) == 1
    assert isinstance(logged[0], RuntimeError)
