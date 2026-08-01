---
name: test-writer
description: Testing standards for Python services — the test pyramid, pytest structure, fixtures, async testing, mocking policy, coverage rules, and what "done" means. Use when writing or reviewing tests, adding a regression test for a bug, deciding what to test or how much, setting up fixtures or testcontainers, when coverage gates fail, or when a test is flaky.
---

# Test Writer

Tests exist to let you change code without fear. A test that does not fail when
the behaviour it describes breaks is worse than no test, because it buys false
confidence at the cost of maintenance.

## The pyramid

| Tier | Location | Count | Speed | Touches |
|---|---|---|---|---|
| **Unit** | `tests/unit/` | ~70% | <10ms each | Pure `domain/` and `core/`. No I/O, no DB, no HTTP, no event loop unless the unit is genuinely async. |
| **Integration** | `tests/integration/` | ~25% | <2s each | One real adapter — real Postgres/Redis via testcontainers. Never mock the database you are testing against. |
| **E2E** | `tests/e2e/` | ~5% | seconds | The whole app through `httpx.AsyncClient`. Happy path plus the top two failure modes per critical flow. Not exhaustive. |

If the pyramid inverts — most of your tests going through the full app — the
suite becomes slow, flaky, and it stops being run. If it collapses to unit only,
you get 95% coverage and a service that cannot connect to its own database.

## The one non-negotiable rule

> **Watch the test fail before you make it pass.**

For a bug fix, the test must fail with the *same symptom the user reported*. A
regression test written after the fix, never observed failing, is an assertion
that the current code does what the current code does.

If a test already exists when you arrive at it, **prove it fails without the
fix** — temporarily revert the change and run it. A test that passes either way
locks nothing in, and reviewing it will not reveal that, because it looks
exactly like a test that works.

## Assert the positive case, not only the negative

A test proving that a stranger gets 403 passes just as happily when **every
legitimate user is locked out too**.

```python
async def test_only_the_owner_can_read(client, owner, stranger, order):
    assert (await client.get(f"/orders/{order.id}", auth=stranger)).status_code == 404
    assert (await client.get(f"/orders/{order.id}", auth=owner)).status_code == 200   # ← the half people skip
```

This generalises to every guard: prove the allowed path still works, not only
that the forbidden one fails. A permission change that over-tightens is as much
an outage as one that over-loosens, and only the second half of that test can
catch it.

## Structure: Arrange–Act–Assert

```python
async def test_cancelling_a_shipped_order_is_rejected() -> None:
    # Arrange
    order = make_order(status=OrderStatus.SHIPPED)
    service = OrderService(orders=InMemoryOrderRepository([order]))

    # Act / Assert
    with pytest.raises(OrderNotCancellable) as exc:
        await service.cancel(order.id, reason="customer changed mind")

    assert exc.value.order_id == order.id
```

- **Name tests as behaviour statements**, not method names.
  `test_cancelling_a_shipped_order_is_rejected` — not `test_cancel_2`.
  The name is what shows up in CI output; make it explain the failure.
- **One behaviour per test.** Multiple asserts are fine when they describe one
  outcome; multiple *acts* mean you have two tests glued together.
- **No conditionals or loops in tests** — except `pytest.mark.parametrize`.
  A test with an `if` in it has an untested branch.

## Fixtures

- Root `tests/conftest.py` holds only genuinely global fixtures.
- Prefer **factory functions** (`make_order(...)`) over fixtures for test data.
  Factories take overrides, so each test states exactly the fields it cares
  about and nothing more; fixtures force you to read elsewhere to know why a
  test passes.
- Scope expensively: `session` for containers, `function` for anything mutable.
  A `session`-scoped mutable fixture creates order-dependent tests, which are
  the most common source of "passes locally, fails in CI".

```python
# tests/factories.py
def make_order(**overrides: Any) -> Order:
    defaults: dict[str, Any] = {
        "id": "ord_test",
        "customer_id": "cus_test",
        "status": OrderStatus.PENDING,
        "total_cents": 1000,
    }
    return Order(**{**defaults, **overrides})
```

## Async testing

```toml
# pyproject.toml
[tool.pytest.ini_options]
asyncio_mode = "auto"          # no @pytest.mark.asyncio boilerplate
```

E2E against the real app, using ASGI transport (no network, no live server):

```python
@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
```

## Mocking policy

Mock **at the port boundary, and nowhere else.**

- ✅ A fake implementing `domain.ports.OrderRepository` — a real class holding a
  dict. Type-checked against the Protocol, so it breaks when the port changes.
- ✅ `respx` for outbound HTTP in integration tests.
- ❌ `unittest.mock.patch` on internal functions. It couples the test to the
  implementation's call graph, so every refactor breaks tests that should not
  care. When you find yourself patching an internal, the real problem is a
  missing seam — add a port instead.
- ❌ Mocking the database. Use testcontainers. A mocked DB tests your mock's
  understanding of SQL, which is exactly the thing that is wrong.

```python
class InMemoryOrderRepository:
    """Satisfies domain.ports.OrderRepository. mypy enforces the match."""

    def __init__(self, seed: list[Order] | None = None) -> None:
        self._items = {o.id: o for o in (seed or [])}

    async def get(self, order_id: str) -> Order | None:
        return self._items.get(order_id)

    async def save(self, order: Order) -> None:
        self._items[order.id] = order
```

## What to test

**Always:**
- Every domain invariant and every state transition, valid and invalid
- Every error path that maps to a distinct HTTP status
- Boundaries: empty, one, many, max; zero, negative, overflow
- Auth: unauthenticated, authenticated-but-unauthorized, authorized
- Idempotency: the same request twice produces one effect
- Every bug ever fixed (that is what a regression test is)

**Never:**
- Third-party library behaviour (that is their test suite)
- Pydantic validating types it defines (that is Pydantic's test suite)
- Trivial getters, `__repr__`, generated code
- Exact log strings — assert on structured fields instead

## Coverage

`--cov-fail-under=85` on `src/`, with **branch coverage on**. Line coverage
alone reports 100% on an `if` whose `else` was never taken.

Coverage is a floor, not a target. 85% with the critical paths covered beats 98%
achieved by testing `__repr__`. Never chase the number by testing trivia.

If a line is genuinely untestable, exclude it explicitly with a reason:

```python
if TYPE_CHECKING:  # pragma: no cover
    from app.domain.ports import OrderRepository
```

**Do not lower the threshold to make CI pass.** If a PR cannot reach 85%, the
answer is more tests or a smaller PR.

## Flaky tests

A flaky test is a broken test. Quarantine it the same day (`@pytest.mark.flaky`
plus a tracking issue) and fix it within the week — never leave it failing
intermittently in the main run, because that teaches the whole team to ignore red.

The usual causes, in frequency order: shared mutable state across tests, real
time (`datetime.now()`), real sleeps, unordered collections asserted as ordered,
and unawaited tasks. Fix with: function-scoped fixtures, `freezegun`/injected
clocks, event-based waits, sorting before comparison, and explicit `await`.

### Two that survive review

**Global counts under parallel workers.** A test asserting
`count(SomeTable) == 1` on a table another route *commits* to — outside the
test's rollback — sees rows from other workers. It is green alone, green on
re-run, and red once a week. Scope the assertion to the test's own key, and make
the key delimiter-safe so one cannot be a prefix of another. **Never assert a
global count of a committed table.**

**Order-dependent leakage.** If the schema is created once per run and only the
transaction is per-test, then the per-test rollback is the *only* isolation, and
anything escaping it leaks into whatever runs next. A single green run cannot
detect this. After any change to the test harness, run the suite **twice and in a
different order** before trusting it.

Session-scoped engines make suites much faster — per-test schema creation issues
a table-existence query per table and can consume a large fraction of runtime —
but the speed is only safe when the rollback boundary genuinely holds.

## Commands

```bash
uv run pytest -q                                   # everything
uv run pytest tests/unit -q                        # fast loop, use while coding
uv run pytest -q -k "cancel and not integration"   # focused
uv run pytest --cov=src --cov-branch --cov-report=term-missing --cov-fail-under=85
uv run pytest -q -p no:randomly --lf                # rerun last failures
```
