"""Every 422 carries `errors: [{pointer, message}]` beside `detail` (#195).

Built on a small app of its own, registered with the real handlers, so each
case needs no database: a body field, a nested list item, a query parameter, a
domain `ValidationError`, and a status that is not a 422 at all.
"""

from __future__ import annotations

from fastapi import FastAPI, Query
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from app.api import errors
from app.core.errors import NotFoundError, ValidationError
from app.main import create_app


class Answer(BaseModel):
    text: str = Field(min_length=1)


class Form(BaseModel):
    question_text: str = Field(max_length=5)
    answers: list[Answer] = []


def client() -> TestClient:
    app = FastAPI()
    errors.register(app)

    @app.post("/forms")
    async def create(form: Form) -> dict[str, str]:
        return {"question_text": form.question_text}

    @app.get("/items")
    async def items(limit: int = Query(ge=1, le=50)) -> dict[str, int]:
        return {"limit": limit}

    @app.get("/domain")
    async def domain() -> None:
        raise ValidationError("that time is not available")

    @app.get("/missing")
    async def missing() -> None:
        raise NotFoundError("no such thing")

    return TestClient(app)


def test_a_body_field_is_pointed_at() -> None:
    body = client().post("/forms", json={"question_text": "far too long"}).json()

    assert body["errors"] == [
        {"pointer": "/question_text", "message": body["errors"][0]["message"]}
    ]
    assert body["errors"][0]["message"]
    assert "detail" in body


def test_a_nested_list_item_is_pointed_at() -> None:
    body = client().post("/forms", json={"question_text": "ok", "answers": [{"text": ""}]}).json()

    assert [e["pointer"] for e in body["errors"]] == ["/answers/0/text"]


def test_a_query_parameter_is_pointed_at_by_location() -> None:
    body = client().get("/items", params={"limit": 99}).json()

    assert [e["pointer"] for e in body["errors"]] == ["/query/limit"]


def test_a_domain_validation_error_has_an_empty_list() -> None:
    """One shape for every 422: a client reads `errors` without checking for it."""
    response = client().get("/domain")

    assert response.status_code == 422
    assert response.json()["errors"] == []
    assert response.json()["detail"] == "that time is not available"


def test_other_statuses_carry_no_errors_list() -> None:
    response = client().get("/missing")

    assert response.status_code == 404
    assert "errors" not in response.json()


def test_the_submitted_value_is_never_echoed() -> None:
    """A message describes the rule, not the input: a password or a token
    sent in the wrong field must not come back in the response."""
    submitted = "a-value-too-long-to-accept"
    body = client().post("/forms", json={"question_text": submitted}).json()

    assert submitted not in str(body["errors"])


def test_a_pointer_escapes_slash_and_tilde() -> None:
    """RFC 6901: `~` is `~0` and `/` is `~1`, so a key holding either is still
    one reference token."""
    assert errors.json_pointer(("a/b", "c~d", 0)) == "/a~1b/c~0d/0"


def test_the_published_422_is_the_problem_shape_with_errors() -> None:
    spec = create_app().openapi()
    schema = spec["components"]["schemas"]["HTTPValidationError"]

    assert {"type", "title", "status", "detail", "errors"} <= set(schema["properties"])
    item = spec["components"]["schemas"]["ValidationError"]
    assert set(item["properties"]) == {"pointer", "message"}
