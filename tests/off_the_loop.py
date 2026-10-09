"""Every outbound call is made off the event loop (#370), enforced by the suites.

Outbound HTTP here is synchronous, so a call made on the event loop holds the
whole worker for the round trip: every other request waits on Google, Daily,
QStash, Loops or storage. The standard (`project-conventions`) is that async
code reaches an adapter through `asyncio.to_thread`.

A static scan cannot hold that line: it misses a blocking call made through a
sync helper, which is how the session reminders' four QStash calls per booking
went unseen. So the doubles do it instead. Anything a test puts on an adapter
seam of `app.state` is wrapped, and a call that arrives on the event loop thread
fails the test, wherever in the call chain it was made.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
from typing import Any

#: The `app.state` names an outbound adapter is read from. One list, so a new
#: seam is guarded by adding its name here.
ADAPTER_SEAMS = frozenset(
    {
        "calendar",
        "calendar_account_email",
        "calendar_exchange",
        "free_busy",
        "intake_storage",
        "meeting_rooms",
        "notifier",
        "scheduler",
        "storage",
    }
)


class BlockedTheLoopError(AssertionError):
    """An outbound adapter was called on the event loop thread."""


def refuse_on_the_loop(what: str) -> None:
    """Fail if the caller is on a thread running an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise BlockedTheLoopError(
        f"{what} was called on the event loop; reach it through asyncio.to_thread (#370)"
    )


def guarded(name: str, value: Any) -> Any:
    """`value`, wrapped so calling it, or any method on it, checks the thread."""
    if value is None or isinstance(value, OffTheLoop) or _is_async(value):
        return value
    if inspect.isroutine(value) or isinstance(value, functools.partial):
        return _checked(name, value)
    return OffTheLoop(name, value)


def _is_async(value: Any) -> bool:
    """A coroutine function is called on the loop by definition, and does its
    own thread hop inside when it needs one (the free/busy reader does)."""
    return inspect.iscoroutinefunction(value)


def _checked(name: str, call: Any) -> Any:
    @functools.wraps(call)
    def checked(*args: Any, **kwargs: Any) -> Any:
        refuse_on_the_loop(name)
        return call(*args, **kwargs)

    return checked


class OffTheLoop:
    """A stand-in's methods, each refusing to run on the event loop.

    Attributes that are not methods pass straight through, so a test reading
    back what its fake recorded (`calendar.conferences`) sees the real list.
    """

    def __init__(self, name: str, target: Any) -> None:
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_target", target)

    def __getattr__(self, attribute: str) -> Any:
        value = getattr(self._target, attribute)
        if callable(value) and not attribute.startswith("_") and not _is_async(value):
            return _checked(f"{self._name}.{attribute}", value)
        return value

    def __setattr__(self, attribute: str, value: Any) -> None:
        setattr(self._target, attribute, value)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        refuse_on_the_loop(self._name)
        return self._target(*args, **kwargs)
