---
name: project-structure
description: Layered/hexagonal directory layout and import rules for Python FastAPI services — where api, domain, infra, core, and observability code belongs and which layer may import which. Use when creating a new file or module, deciding where code should live, scaffolding a new service, reviewing architecture, resolving a circular import, or when a layer-boundary check fails.
---

# Project Structure

A layered (ports-and-adapters) layout. One rule carries most of the value:

> **`domain/` imports nothing from `api/`, `infra/`, or any web/DB framework.**

Everything else follows from it. That rule is machine-enforced by
`scripts/check_layers.py` in CI — it is not a guideline.

## Layout

```
src/app/
├── main.py              # ASGI app factory + wiring. The ONLY place layers meet.
├── api/                 # Transport. HTTP in, HTTP out. No business logic.
│   ├── deps.py          # FastAPI dependency providers (auth, db session, repos)
│   ├── errors.py        # domain exception -> HTTP status mapping
│   ├── schemas/         # Pydantic request/response models (the wire contract)
│   └── routes/          # APIRouter modules, one per resource
├── domain/              # The product. Pure Python. No I/O, no framework.
│   ├── models.py        # entities & value objects (dataclass / plain Pydantic)
│   ├── errors.py        # domain exceptions
│   ├── ports.py         # Protocol interfaces that infra must satisfy
│   └── services.py      # use cases / business rules
├── infra/               # Adapters. Everything that talks to the outside world.
│   ├── db/              # SQLAlchemy models, session, repositories, migrations
│   ├── cache/
│   └── clients/         # outbound HTTP/gRPC/queue clients
├── core/                # Cross-cutting primitives, dependency-free
│   ├── config.py        # pydantic-settings; the ONLY reader of the environment
│   └── errors.py        # base exception hierarchy
└── observability/       # logging, tracing, metrics setup
```

## The dependency rule

```
api ──────► domain ◄────── infra
 │                            │
 └──────────► core ◄──────────┘
```

| Layer | May import | May NOT import |
|---|---|---|
| `api` | `domain`, `core`, `observability`, FastAPI/Pydantic | `infra` (except in `deps.py` wiring) |
| `domain` | `core`, stdlib, Pydantic | `api`, `infra`, FastAPI, SQLAlchemy, httpx |
| `infra` | `domain` (to implement its ports), `core` | `api` |
| `core` | stdlib, pydantic-settings | everything in this project |
| `observability` | `core` | `api`, `domain`, `infra` |

`api/deps.py` and `main.py` are the two sanctioned exceptions — they exist
precisely to be the composition root where concrete `infra` classes get bound to
`domain` ports.

## Why `domain` must stay pure

- **Testability**: domain tests need no database, no event loop, no fixtures.
  They run in milliseconds, so they get run.
- **Replaceability**: swapping Postgres for DynamoDB, or REST for gRPC, touches
  one layer instead of the whole tree.
- **Comprehensibility**: business rules are readable without knowing the web
  framework.

When `domain` imports SQLAlchemy, every one of those properties is lost at once,
and it is never recovered incrementally. This is why the check is a hard CI gate
rather than a review convention — review catches it maybe 70% of the time, and
the 30% is enough to erode the boundary within a quarter.

## Ports and adapters, concretely

```python
# domain/ports.py — the domain declares what it needs
from typing import Protocol
from app.domain.models import Order

class OrderRepository(Protocol):
    async def get(self, order_id: str) -> Order | None: ...
    async def save(self, order: Order) -> None: ...
```

```python
# domain/services.py — depends on the Protocol, never a concrete class
class OrderService:
    def __init__(self, orders: OrderRepository) -> None:
        self._orders = orders
```

```python
# infra/db/repositories.py — the adapter satisfies the port
class SqlOrderRepository:
    def __init__(self, session: AsyncSession) -> None: ...
    async def get(self, order_id: str) -> Order | None: ...
    async def save(self, order: Order) -> None: ...
```

```python
# api/deps.py — the composition root binds them
async def get_order_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> OrderService:
    return OrderService(orders=SqlOrderRepository(session))
```

Note there is no DI framework. `Protocol` plus FastAPI's `Depends` is sufficient
and adds no dependency.

## Naming and file conventions

- Modules and packages: `lower_snake_case`. Classes: `PascalCase`.
- One resource per route module: `api/routes/orders.py` → `/orders`.
- Schemas are named for their direction: `OrderCreate`, `OrderUpdate`,
  `OrderRead`. Never reuse one model for both request and response — that couples
  your write contract to your read contract and eventually leaks internal fields.
- SQLAlchemy models live only in `infra/db/models.py` and never leave `infra`.
  Repositories translate DB rows ↔ domain entities at the boundary.
- Tests mirror the source tree: `tests/unit/domain/test_services.py`.

## Where new code goes — decision table

| You are adding… | It goes in |
|---|---|
| A new endpoint | `api/routes/<resource>.py` + `api/schemas/<resource>.py` |
| A business rule or invariant | `domain/services.py` or `domain/models.py` |
| A DB query | `infra/db/repositories.py` (behind a port in `domain/ports.py`) |
| A call to a third-party API | `infra/clients/<vendor>.py` (behind a port) |
| A config value | `core/config.py` — nowhere else reads the environment |
| A new error type | `domain/errors.py`, then map it in `api/errors.py` |
| A background job | `infra/` for the trigger, `domain/services.py` for the logic |
| A shared helper | Resist. Put it next to its only caller until there are three. |

## Vendor isolation

Third-party SDKs are imported in **one package only**. Feature code depends on a
capability (`from app.providers.sms import send_sms`), never on a vendor name.

Enforce it mechanically — a lint or grep in CI that fails the build on a vendor
import outside the provider package. Otherwise the first exception under deadline
becomes the precedent, and the abstraction quietly stops being one.

## Commenting norms

Comments are a **first-class deliverable on every build**, not an afterthought.
The standard: a developer who has never seen this codebase can open any model,
service, or migration and understand what it does, why it exists, what invariants
it enforces, and what to watch out for.

**Always gets a docstring:**

- **Model classes** — what the table represents in the domain; **single-row vs
  multi-row semantics** (the most common source of confusion); key invariants
  such as soft-delete or lifecycle states; any naming decision that could mislead;
  relationships whose direction is non-obvious.
- **Service functions with non-trivial logic** — transactional, idempotent,
  side-effecting, or enforcing a non-obvious invariant. Plain CRUD getters can
  skip it.
- **Migrations** — inline comments on each operation explaining *why* it is
  needed and why it appears in that position: why the FK is added after the
  rename, why the backfill runs before the drop.

**Inline comments explain WHY, never WHAT.** Well-named identifiers already say
what. Comment the invariant being protected, the deliberate choice between two
valid approaches, the workaround for a known engine quirk, or the behaviour that
would surprise a careful reader.

```python
# Guard is first so "off" is provably a no-op — anything above this line
# runs even when the feature is disabled.
if not settings.SYNC_ENABLED:
    return
```

Do not comment simple getters, standard route handlers, or lines whose purpose is
evident from the function name.

## Detailed reference

`references/layout.md` has the full annotated tree, the `check_layers.py`
enforcement script, and worked migration notes for retrofitting an existing
flat service into this structure.
