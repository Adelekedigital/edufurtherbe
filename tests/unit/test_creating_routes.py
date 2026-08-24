"""Every route that creates something tells the caller where it went.

**A source walk, because no gate can see this rule.** `Location` is set inside a
handler body at runtime; it is not a type, not a decorator argument, and not
anything ruff or mypy binds. Seven of the nine creating routes had it and two did
not, and nothing anywhere compared the two lists — the same shape as the OpenAPI
tag check, which exists because three tags shipped undescribed for the same
reason.

Asserting the *rule* rather than the nine known cases is the point: a tenth
creating route is exactly the one that would be missed, and it is the one this
fails on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROUTES = Path(__file__).resolve().parents[2] / "src" / "app" / "api" / "routes"

#: Split on the decorator rather than matched with one expression. A single
#: regex spanning decorator, signature and body is the kind that is easier to
#: get subtly wrong than the thing it checks — and a test nobody trusts is worse
#: than no test.
DECORATOR = "@router."


def creating_handlers() -> list[tuple[str, str, str]]:
    """``(file, handler, source)`` for every route declaring a 201.

    ``source`` is the decorator *and* the body: the status code lives in the
    first and the header in the second, so a block that stops at the signature
    could not see both.
    """
    found: list[tuple[str, str, str]] = []
    for path in sorted(ROUTES.glob("*.py")):
        blocks = path.read_text(encoding="utf-8").split(DECORATOR)
        for block in blocks[1:]:
            if "HTTP_201_CREATED" not in block:
                continue
            name = next(
                (
                    line.split("(")[0].removeprefix("async def ").removeprefix("def ").strip()
                    for line in block.splitlines()
                    if line.startswith(("async def ", "def "))
                ),
                "<unnamed>",
            )
            found.append((path.name, name, block))
    return found


def test_there_are_creating_routes_to_check() -> None:
    """A walk that finds nothing passes every assertion below it.

    The same guard the ADR and settled-decision checks open with, and for the
    same reason: this file is pointed at a directory by a path, and a path that
    stops resolving turns the whole test green.
    """
    assert len(creating_handlers()) >= 9


@pytest.mark.parametrize(
    "handler", creating_handlers(), ids=lambda h: f"{h[0].removesuffix('.py')}.{h[1]}"
)
def test_a_creating_route_sends_a_location(handler: tuple[str, str, str]) -> None:
    """`201` without `Location` makes a client guess where the thing went.

    Two of the nine omitted it — `POST /sessions` and `POST /reviews` — while the
    other seven set it, so the convention was settled by weight of precedent and
    only the outliers were unaware of it.
    """
    name, function, block = handler

    assert 'headers["Location"]' in block, (
        f"{name}::{function} returns 201 without setting Location. Seven other "
        "creating routes set it; a client that has to guess the URL of what it "
        "just made is being told less than the response could tell it."
    )
