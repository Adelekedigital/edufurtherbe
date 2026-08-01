---
name: api-designer
description: HTTP API contract design for FastAPI services — resource naming, versioning, request/response schemas, error envelopes, pagination, filtering, idempotency, and deprecation. Use when adding or changing an endpoint, designing a new API surface, choosing status codes, deciding whether a change is breaking, reviewing an OpenAPI schema, or when a client integration keeps needing clarification.
---

# API Designer

The contract is the product. Internals can be refactored freely; a published
contract cannot. Design the boundary before the body — see `build-workflow`
Phase 1.

In FastAPI the Pydantic models **are** the contract: OpenAPI is generated from
them, so a sloppy model is a sloppy public API.

## Resources and naming

- **Plural nouns, not verbs.** `/orders`, not `/getOrders`. The verb is the HTTP
  method.
- Nest only to express ownership, and only one level: `/orders/{id}/items`.
  Deeper nesting encodes a hierarchy that will change. Prefer
  `/items?order_id=…` beyond one level.
- `kebab-case` in paths, `snake_case` in JSON bodies. Pick one and never mix —
  clients write serializers against it.
- Actions that are genuinely not CRUD get a sub-resource verb:
  `POST /orders/{id}/cancel`. This is better than overloading `PATCH` with a
  magic `status` field, because the permission model and validation differ.

## Methods and status codes

| Method | Semantics | Success |
|---|---|---|
| `GET` | Read, no side effects, cacheable | 200, 404 |
| `POST` | Create, or a non-idempotent action | 201 + `Location`, 200 for actions |
| `PUT` | Full replace, idempotent | 200 / 204 |
| `PATCH` | Partial update | 200 |
| `DELETE` | Remove, idempotent | 204 |

| Status | Means | Common mistake |
|---|---|---|
| 400 | Malformed request | Using it for validation failures — that is 422 |
| 401 | Not authenticated | Returning it when the caller *is* authenticated but unauthorized |
| 403 | Authenticated, not permitted | Leaking existence — use 404 for objects the caller may not see |
| 404 | Not found, or not visible to this caller | — |
| 409 | State conflict (already cancelled) | Using 400 for a valid request against invalid state |
| 422 | Validation failed | — |
| 429 | Rate limited | Omitting `Retry-After` |

**Never return 200 with an error body.** Clients branch on status; an error
inside a 200 will be treated as success by every generated client.

## Request and response models

Separate models per direction, always:

```python
class OrderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_id: str = Field(min_length=1, max_length=64)
    total_cents: int = Field(ge=0, le=100_000_000)

class OrderRead(BaseModel):
    id: str
    customer_id: str
    status: OrderStatus
    total_cents: int
    created_at: datetime
```

- **`extra="forbid"` on every request model.** Silently ignoring unknown fields
  hides client typos and accepts mass-assignment attempts.
- **Response models are explicit allowlists.** Never serialize an ORM or domain
  object directly — `password_hash` has shipped that way more than once.
- **Constrain everything**: `min_length`, `max_length`, `ge`, `le`, patterns. An
  unconstrained `str` is an unbounded write.
- **Timestamps are ISO-8601 UTC strings.** Not epoch integers — clients then need
  out-of-band knowledge of the unit and timezone.
- **Money is integer minor units** (`total_cents`), never a float. Include the
  currency when more than one is possible.
- **Enums, not free strings**, for closed sets. They appear in OpenAPI and
  generate typed clients.
- Prefixed, non-sequential IDs (`ord_01H…`). Sequential integers let anyone
  enumerate your entire table by counting.

## Versioning

- Version in the path: `/v1/orders`. Header versioning is more elegant and much
  harder for clients to debug, cache, and log.
- **Additive changes do not need a new version.** A new version is for breaking
  changes only — see the SemVer rules in `release-notes`.
- Run `/v1` and `/v2` side by side during migration. Do not fork the domain
  layer: both versions map to the same services, differing only in `api/schemas`.

What counts as breaking (people routinely get these wrong):

| Change | Breaking? |
|---|---|
| Adding an optional request field | No |
| Adding a **required** request field | **Yes** |
| Adding a response field | No, if clients tolerate unknown fields — document that expectation |
| Removing or renaming a response field | **Yes** |
| Tightening validation | **Yes** — requests that used to succeed now 422 |
| Loosening validation | No |
| Changing a default value | **Yes**, if behaviour changes for callers who omit it |
| Adding a new enum value in a response | **Yes** in practice — strict clients fail to parse |
| Changing a status code | **Yes** |

## Error envelope

One shape, every error, every endpoint:

```json
{
  "code": "order_not_cancellable",
  "message": "Order cannot be cancelled while shipped",
  "request_id": "01H8X…"
}
```

- `code` is **stable and machine-readable**. Clients branch on it, so treat it
  as part of the contract — renaming one is a breaking change.
- `message` is for humans and logs. Never put internal detail, SQL, stack
  traces, or PII in it.
- `request_id` on every error so support tickets carry the exact search key.
- Field-level validation errors get a `details` array with `field` and `code`
  per entry, so clients can highlight the offending input.

Map domain exceptions to status codes in **one place** (`api/errors.py`).
Scattered translation drifts.

## Pagination

Default to **cursor-based** for anything that grows. Offset pagination skips and
duplicates rows when the underlying data changes between pages, and gets slower
the deeper you go.

```json
{
  "data": [ ... ],
  "next_cursor": "eyJpZCI6…",
  "has_more": true
}
```

- **Hard maximum page size**, not just a default. `?limit=1000000` is a denial of
  service otherwise.
- The cursor is opaque. Clients must not construct or parse it — that lets you
  change the underlying key later.
- Always wrap collections in an object. A bare top-level JSON array leaves you
  nowhere to add pagination metadata without breaking clients.

## Filtering and sorting

```
GET /v1/orders?status=pending&created_after=2026-01-01&sort=-created_at&limit=50
```

- **Sort fields must be an allowlist**, mapped to columns explicitly. Passing
  user input into `ORDER BY` is SQL injection, and it is the variant people miss
  because it does not look like a query string concatenation.
- Every filterable field needs a supporting index, or it is an outage at scale.
- `-field` for descending is the common convention; document it.

## Idempotency

Any non-GET endpoint that creates, charges, or sends must be safe to retry —
networks fail after the server commits but before the client sees the response.

```
POST /v1/orders
Idempotency-Key: 01H8XABCDEF
```

- Store the key with the response for 24h+; a repeat returns the **stored**
  response without re-executing.
- Scope keys per endpoint and per caller.
- A repeat with the same key but a *different* body is a 422 — that is a client
  bug worth surfacing loudly.
- `PUT` and `DELETE` should be naturally idempotent. A second `DELETE` returns
  204, not 404 — the desired state is achieved either way.

## Deprecation

Never remove without a staged path:

1. **Announce** — mark `deprecated=True` in the route decorator so it shows in
   OpenAPI; add a `Deprecation` and `Sunset` header.
2. **Measure** — log usage by caller. You cannot remove what you cannot see.
3. **Notify** — contact remaining callers directly. A changelog entry is not
   notification.
4. **Remove** — only after usage is zero, past the sunset date.

```python
@router.get("/v1/orders/{order_id}", deprecated=True)
```

## Documentation

FastAPI generates OpenAPI, but only as well as you annotate:

```python
@router.post(
    "/v1/orders",
    status_code=201,
    response_model=OrderRead,
    summary="Create an order",
    responses={
        409: {"model": ErrorResponse, "description": "Duplicate idempotency key"},
        422: {"model": ErrorResponse, "description": "Validation failed"},
    },
)
```

- Declare **every** status code the endpoint can return. Undocumented errors are
  the top source of client integration bugs.
- Use `Field(description=…, examples=[…])` — examples reach the docs and the
  generated clients.
- Disable `/docs` and `/openapi.json` in production unless the API is public.

## Checklist for a new endpoint

- [ ] Plural noun path, correct method, correct status codes
- [ ] Separate request/response models; `extra="forbid"` on requests
- [ ] Response model is an explicit field allowlist
- [ ] Every field constrained; enums for closed sets
- [ ] Authorization scoped **in the query**; 404 not 403 for invisible objects
- [ ] Errors use the standard envelope with a stable `code`
- [ ] Collections paginated with a hard max page size
- [ ] Sort/filter fields allowlisted and indexed
- [ ] `Idempotency-Key` supported if it creates, charges, or sends
- [ ] Rate limit appropriate to the cost of the operation
- [ ] All response codes declared in the OpenAPI annotation
- [ ] Breaking-change assessment done against the table above
