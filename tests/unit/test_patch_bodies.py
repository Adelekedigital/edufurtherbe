"""A PATCH body publishes no defaults, so a generated client sends only what changed.

A field with a `default` in the spec is typed as always present by the common
TypeScript generators (openapi-typescript's `defaultNonNullable`), so the
frontend read `cover_art` and `timezone` as required on `PATCH /profile` and
would have had to resend the current timezone to change a colour. On a PATCH
the default is never used: the writer takes `exclude_unset`.
"""

from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.main import create_app


def spec() -> dict[str, Any]:
    return dict(create_app(Settings(_env_file=None)).openapi())


def test_the_profile_patch_publishes_no_defaults() -> None:
    body = spec()["components"]["schemas"]["UserProfileWrite"]

    defaulted = sorted(
        name for name, prop in body["properties"].items() if prop.get("default") is not None
    )

    assert body.get("required", []) == []
    assert defaulted == []
