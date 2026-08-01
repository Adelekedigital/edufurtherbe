# Layout Reference

Full annotated tree, the enforcement script, and retrofit guidance.

## Annotated tree

```
.
├── pyproject.toml            # single source of truth for deps + all tool config
├── uv.lock                   # committed; reproducible installs
├── CLAUDE.md                 # thin router for agents (~90 lines, no prose dumps)
├── .pre-commit-config.yaml
├── .github/workflows/
│   ├── ci.yml                # lint -> types -> layers -> tests -> coverage gate
│   ├── security.yml          # bandit, pip-audit, gitleaks, CodeQL
│   └── release.yml           # tag -> changelog -> artifact
├── docs/
│   ├── architecture.md       # the map: context, containers, key flows
│   └── adr/                  # numbered decision records (see the `adr` skill)
├── scripts/
│   └── check_layers.py       # architectural import boundary enforcement
├── src/app/
│   ├── __init__.py
│   ├── main.py
│   ├── api/
│   │   ├── deps.py
│   │   ├── errors.py
│   │   ├── routes/
│   │   │   ├── __init__.py   # aggregates routers; mounted by main.py
│   │   │   ├── health.py     # /healthz (liveness) + /readyz (readiness)
│   │   │   └── orders.py
│   │   └── schemas/
│   │       └── orders.py
│   ├── domain/
│   │   ├── models.py
│   │   ├── ports.py
│   │   ├── services.py
│   │   └── errors.py
│   ├── infra/
│   │   ├── db/
│   │   │   ├── models.py     # SQLAlchemy; never leaves this package
│   │   │   ├── session.py
│   │   │   ├── repositories.py
│   │   │   └── migrations/   # alembic
│   │   └── clients/
│   ├── core/
│   │   ├── config.py
│   │   └── errors.py
│   └── observability/
│       ├── logging.py
│       ├── metrics.py
│       └── tracing.py
└── tests/
    ├── conftest.py           # root fixtures only
    ├── unit/                 # no I/O, no event loop needed, milliseconds
    ├── integration/          # real Postgres/Redis via testcontainers
    └── e2e/                  # full app through httpx.AsyncClient
```

## `src/` layout, not flat

The `src/` prefix is deliberate. Without it, `pytest` imports your package from
the working directory rather than from the installed distribution, so tests can
pass locally against files that were never packaged and then fail in production.
`src/` makes the installed artifact the thing under test.

## The enforcement script

`scripts/check_layers.py` walks the AST of every module under `src/` and asserts
the import matrix. It runs in pre-commit and in CI.

```python
"""Enforce architectural import boundaries. Exit non-zero on violation."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

SRC = Path("src/app")

# layer -> set of sibling layers it may import
ALLOWED: dict[str, set[str]] = {
    "api": {"domain", "core", "observability"},
    "domain": {"core"},
    "infra": {"domain", "core", "observability"},
    "core": set(),
    "observability": {"core"},
}

# Third-party packages that must never appear inside domain/.
DOMAIN_FORBIDDEN_EXTERNAL = {
    "fastapi", "starlette", "sqlalchemy", "alembic", "httpx",
    "requests", "redis", "boto3", "celery",
}

# Composition roots: allowed to import anything, by design.
EXEMPT = {SRC / "main.py", SRC / "api" / "deps.py"}


def layer_of(path: Path) -> str | None:
    rel = path.relative_to(SRC)
    return rel.parts[0] if len(rel.parts) > 1 else None


def imported_modules(tree: ast.AST) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(a.name, node.lineno) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.append((node.module, node.lineno))
    return out


def main() -> int:
    violations: list[str] = []

    for path in SRC.rglob("*.py"):
        if path in EXEMPT:
            continue
        layer = layer_of(path)
        if layer is None or layer not in ALLOWED:
            continue

        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module, lineno in imported_modules(tree):
            root = module.split(".")[0]

            if module.startswith("app."):
                parts = module.split(".")
                if len(parts) < 2:
                    continue
                target = parts[1]
                if target != layer and target not in ALLOWED[layer]:
                    violations.append(
                        f"{path}:{lineno}: '{layer}' may not import '{target}' "
                        f"(allowed: {sorted(ALLOWED[layer]) or 'none'})"
                    )
            elif layer == "domain" and root in DOMAIN_FORBIDDEN_EXTERNAL:
                violations.append(
                    f"{path}:{lineno}: domain/ must stay framework-free; "
                    f"remove the '{root}' import and express the need as a "
                    f"Protocol in domain/ports.py"
                )

    if violations:
        print("Architectural boundary violations:\n", file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        print(
            "\nSee the project-structure skill. Do not weaken ALLOWED to pass.",
            file=sys.stderr,
        )
        return 1

    print("Layer boundaries OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

## Retrofitting an existing flat service

Do it in this order. Each step is independently shippable and independently
revertable — resist the urge to do it as one big-bang PR.

1. **Add the directories and the checker, with `ALLOWED` set permissively.**
   No code moves. The checker passes trivially. This gets the tooling merged
   while the diff is boring.
2. **Extract `core/config.py`.** Find every `os.environ` / `os.getenv` read and
   route it through one pydantic-settings object. This is usually the single
   highest-value step and it is low risk.
3. **Extract `domain/models.py`.** Create plain entities alongside the ORM
   models. Do not delete the ORM models. Repositories translate between them.
4. **Introduce `domain/ports.py` Protocols** for each data access pattern the
   business logic uses, and make existing DB code satisfy them.
5. **Move business logic out of route handlers into `domain/services.py`.**
   Handlers shrink to: parse → call service → map result. Do one resource per PR.
6. **Tighten `ALLOWED`** one layer at a time, starting with `domain`. Turning on
   `DOMAIN_FORBIDDEN_EXTERNAL` last is usually the right call, because it is the
   noisiest.

Expect step 5 to be ~80% of the effort. Steps 1–4 are mechanical; step 5 is where
you discover which "business rules" were actually transport concerns.
