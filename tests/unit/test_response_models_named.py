"""Every JSON success response names its keys (issue #430).

A handler annotated ``-> dict[...]`` publishes an object whose only shape is
``additionalProperties``: the spec cannot say which keys arrive, so a generated
client types the body as a bare map. This walks the built spec and fails on any
2xx ``application/json`` schema that is such a map.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import Settings
from app.main import create_app


def _resolve(schema: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = spec["components"]["schemas"][name]
    return schema


PRIMITIVES = {"string", "integer", "number", "boolean", "null"}


def _is_untyped(schema: dict[str, Any], spec: dict[str, Any]) -> bool:
    """True for any body whose keys the spec cannot name: a bare map, an empty
    schema (no return annotation), `Any`, or an array of any of those."""
    schema = _resolve(schema, spec)
    for union in ("anyOf", "oneOf", "allOf"):
        if union in schema:
            return any(_is_untyped(variant, spec) for variant in schema[union])
    kind = schema.get("type")
    if kind in PRIMITIVES:
        return False
    if kind == "array":
        return _is_untyped(schema.get("items", {}), spec)
    return not schema.get("properties")


def _untyped_responses(spec: dict[str, Any]) -> list[str]:
    found = []
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            for code, response in operation.get("responses", {}).items():
                if not code.startswith("2"):
                    continue
                body = response.get("content", {}).get("application/json")
                if body is None or "schema" not in body:
                    continue
                if _is_untyped(body["schema"], spec):
                    found.append(f"{method.upper()} {path} {code}")
    return found


def _spec() -> dict[str, Any]:
    return create_app(Settings(_env_file=None)).openapi()


def test_every_json_success_response_names_its_keys() -> None:
    assert _untyped_responses(_spec()) == []


def test_the_guard_sees_an_untyped_map() -> None:
    spec = {
        "components": {"schemas": {"Ack": {"type": "object", "properties": {"ok": {}}}}},
        "paths": {
            "/map": {
                "post": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "additionalProperties": {"type": "boolean"},
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "/named": {
                "post": {
                    "responses": {
                        "200": {
                            "content": {
                                "application/json": {"schema": {"$ref": "#/components/schemas/Ack"}}
                            }
                        }
                    }
                }
            },
        },
    }
    assert _untyped_responses(spec) == ["POST /map 200"]


@pytest.mark.parametrize(
    "schema",
    [
        {},  # no return annotation
        {"title": "Response Thing"},  # -> Any
        {"type": "object", "additionalProperties": True},  # -> dict
        {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        {"anyOf": [{"type": "object", "additionalProperties": True}, {"type": "null"}]},
    ],
)
def test_every_untyped_shape_is_caught(schema: dict[str, Any]) -> None:
    assert _is_untyped(schema, {"components": {"schemas": {}}})


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "#/components/schemas/Ack"},
        {"type": "array", "items": {"$ref": "#/components/schemas/Ack"}},
        {"anyOf": [{"$ref": "#/components/schemas/Ack"}, {"type": "null"}]},
        {"type": "string"},
    ],
)
def test_named_and_primitive_shapes_pass(schema: dict[str, Any]) -> None:
    spec = {"components": {"schemas": {"Ack": {"type": "object", "properties": {"ok": {}}}}}}

    assert not _is_untyped(schema, spec)
