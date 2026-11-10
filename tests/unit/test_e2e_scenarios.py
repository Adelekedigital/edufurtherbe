"""`docs/e2e/scenarios.yaml` stays in step with its schema and with the code.

The scenarios are run by agents against staging, so a path the API no longer
serves or a step naming an account nobody seeds fails here, in CI, rather than
on launch day. The schema is read from `scenario.schema.json` and applied by a
small validator below (no `jsonschema` dependency): the JSON file stays the one
representation of the shape, for the agents and for this test alike.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.infra.db.demo_seed import DEMO_DOMAIN, E2E_ACCOUNTS
from app.main import create_app

E2E = Path("docs/e2e")
DOC = yaml.safe_load((E2E / "scenarios.yaml").read_text(encoding="utf-8"))
SCHEMA = json.loads((E2E / "scenario.schema.json").read_text(encoding="utf-8"))
SCENARIOS: list[dict[str, Any]] = DOC["scenarios"]
SEED: dict[str, Any] = DOC["seed"]["accounts"]
#: A step may run as nobody: a public read, or the refusal of one.
ANONYMOUS = "anonymous"
#: Under `for_each`, the account the scenario is being run for.
EACH = "each"

_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "integer": int,
}


def _resolve(ref: str) -> dict[str, Any]:
    node: Any = SCHEMA
    for part in ref.removeprefix("#/").split("/"):
        node = node[part]
    return node  # type: ignore[no-any-return]


def errors(value: Any, schema: dict[str, Any], at: str = "$") -> list[str]:
    """The subset of JSON Schema `scenario.schema.json` uses, and nothing more."""
    if "$ref" in schema:
        return errors(value, _resolve(schema["$ref"]), at)
    found: list[str] = []
    kind = schema.get("type")
    if kind and not (
        isinstance(value, _TYPES[kind]) and not (kind == "integer" and isinstance(value, bool))
    ):
        return [f"{at}: expected {kind}"]
    if "const" in schema and value != schema["const"]:
        found.append(f"{at}: expected {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        found.append(f"{at}: {value!r} not in {schema['enum']}")
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            found.append(f"{at}: {value!r} does not match {schema['pattern']}")
        if len(value) < schema.get("minLength", 0):
            found.append(f"{at}: too short")
    number = isinstance(value, int) and not isinstance(value, bool)
    if number and not schema.get("minimum", value) <= value <= schema.get("maximum", value):
        found.append(f"{at}: {value} out of range")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            found.append(f"{at}: too few items")
        for n, item in enumerate(value):
            found += errors(item, schema.get("items", {}), f"{at}[{n}]")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        found += [f"{at}: missing {key}" for key in schema.get("required", []) if key not in value]
        if len(value) < schema.get("minProperties", 0):
            found.append(f"{at}: empty")
        for key, item in value.items():
            if key in props:
                found += errors(item, props[key], f"{at}.{key}")
            elif schema.get("additionalProperties") is False:
                found.append(f"{at}: unexpected {key}")
    if "oneOf" in schema:
        matching = [option for option in schema["oneOf"] if not errors(value, option, at)]
        if len(matching) != 1:
            found.append(f"{at}: matches {len(matching)} of oneOf, not exactly one")
    return found


def _all_steps() -> list[tuple[str, dict[str, Any]]]:
    return [(s["id"], step) for s in SCENARIOS for step in s["steps"]]


def test_the_validator_rejects_what_the_schema_forbids() -> None:
    """The validator itself, on the positive and the negative case."""
    good = {
        "id": "X-001",
        "title": "t",
        "journey": "j",
        "priority": "P2",
        "roles": ["mentee"],
        "why": "w",
        "smoke_prod": False,
        "steps": [],
    }
    assert errors(good, SCHEMA) == []
    assert errors({**good, "id": "x-1"}, SCHEMA)
    assert errors({**good, "priority": "P3"}, SCHEMA)
    assert errors({**good, "css": ".btn"}, SCHEMA)
    bad_step = {"kind": "ui", "id": "a", "viewer": "v", "route": "/", "human": "h"}
    assert errors(
        {**good, "steps": [{**bad_step, "actions": [], "expect": [{"css": "x"}]}]}, SCHEMA
    )


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.get("id", "?"))
def test_every_scenario_matches_the_schema(scenario: dict[str, Any]) -> None:
    assert errors(scenario, SCHEMA) == []


def test_scenario_ids_are_unique() -> None:
    ids = [s["id"] for s in SCENARIOS]
    assert len(ids) == len(set(ids))


def test_step_ids_are_unique_within_a_scenario() -> None:
    for scenario in SCENARIOS:
        ids = [step["id"] for step in scenario["steps"]]
        ids += [a["id"] for a in scenario.get("assertions", [])]
        assert len(ids) == len(set(ids)), scenario["id"]


def test_every_after_names_an_earlier_step_and_every_viewer_a_seed_account() -> None:
    for scenario in SCENARIOS:
        assert set(scenario.get("for_each", [])) <= set(SEED), scenario["id"]
        viewers = {*SEED, ANONYMOUS, *([EACH] if "for_each" in scenario else [])}
        seen: set[str] = set()
        for item in [*scenario["steps"], *scenario.get("assertions", [])]:
            if "after" in item:
                assert item["after"] in seen, f"{scenario['id']}.{item['id']}: {item['after']}"
            if "viewer" in item:
                assert item["viewer"] in viewers, f"{scenario['id']}.{item['id']}"
            seen.add(item["id"])


def test_every_job_step_runs_a_script_that_exists() -> None:
    for scenario in SCENARIOS:
        for step in scenario["steps"]:
            if step["kind"] == "job":
                assert Path(step["script"]).is_file(), f"{scenario['id']}.{step['id']}"


def test_seed_accounts_are_the_ones_the_seed_creates() -> None:
    """The YAML names accounts by ref; `E2E_ACCOUNTS` is what `--e2e` seeds and
    what `scripts/e2e_session.py` will sign in. One list, pinned both ways."""
    assert set(SEED) == set(E2E_ACCOUNTS)


def test_every_api_step_is_a_route_the_app_serves() -> None:
    paths = create_app().openapi()["paths"]
    for scenario in SCENARIOS:
        calls = [s for s in scenario["steps"] if s["kind"] == "api"]
        calls += [a for a in scenario.get("assertions", []) if a["check"] == "api"]
        for call in calls:
            where = f"{scenario['id']}.{call['id']}"
            assert call["path"] in paths, f"{where}: {call['path']} is not served"
            method = call.get("method", "GET").lower()
            assert method in paths[call["path"]], f"{where}: {method} {call['path']}"


#: A statement that writes, anywhere in a query, data-modifying CTEs included.
#: The runner also opens `BEGIN READ ONLY` (README), so this is the first lock.
WRITES = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|TRUNCATE|ALTER|DROP|GRANT|CREATE|COPY)\b", re.IGNORECASE
)


def read_only_problem(query: str) -> str | None:
    """Why a db assertion is not a single read, or `None` when it is."""
    text = query.strip()
    if not text.upper().startswith(("SELECT", "WITH")):
        return "not a SELECT"
    if ";" in text.rstrip(";"):
        return "more than one statement"
    # Keywords, not values: `reason = 'grant'` is a read.
    if found := WRITES.search(re.sub(r"'[^']*'", "''", text)):
        return f"writes ({found.group(1)})"
    return None


def test_db_assertions_only_read() -> None:
    for scenario in SCENARIOS:
        for check in scenario.get("assertions", []):
            if check["check"] == "db":
                assert read_only_problem(check["query"]) is None, check["id"]


@pytest.mark.parametrize(
    "query",
    [
        "WITH d AS (DELETE FROM users RETURNING id) SELECT count(*) FROM d",
        "SELECT 1; DROP TABLE users",
        "UPDATE users SET email = 'x'",
        "SELECT * FROM users WHERE id IN (SELECT id FROM t) FOR UPDATE",
        "with x as (insert into t values (1) returning 1) select * from x",
    ],
)
def test_a_query_that_writes_is_refused(query: str) -> None:
    assert read_only_problem(query) is not None


def test_a_plain_read_passes() -> None:
    assert read_only_problem("SELECT count(*) AS n FROM sessions WHERE id = :s") is None
    assert read_only_problem("SELECT 1 FROM t WHERE reason = 'grant'") is None


#: Shapes that look like a credential: a JWT, a Stripe/PostHog/Supabase key.
SECRET_SHAPES = re.compile(r"eyJ[A-Za-z0-9_-]{8,}|\bsk_[A-Za-z0-9]|\bphx_|\bsbp_|service_role\.")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


@pytest.mark.parametrize("name", ["scenarios.yaml", "README.md", "scenario.schema.json"])
def test_nothing_in_the_plan_looks_like_a_secret(name: str) -> None:
    """The repository is public: no token, key or real address may land here."""
    text = (E2E / name).read_text(encoding="utf-8")
    assert not SECRET_SHAPES.search(text), name
    strangers = [m for m in EMAIL.findall(text) if not m.endswith(f"@{DEMO_DOMAIN}")]
    assert strangers == [], name


def test_the_secret_check_catches_what_it_is_for() -> None:
    # Built at runtime, so the scanner sees no secret-shaped literal here.
    assert SECRET_SHAPES.search("token: " + "eyJ" + "a" * 12)
    assert SECRET_SHAPES.search("key: " + "phx" + "_abc")
    assert not SECRET_SHAPES.search("SUPABASE_SERVICE_ROLE_KEY")
    assert EMAIL.findall("someone@gmail.com") == ["someone@gmail.com"]
