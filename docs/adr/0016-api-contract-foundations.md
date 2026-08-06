# 16. API contract foundations: Problem Details, cursor pagination, normalisation at the boundary

Date: 2026-08-06

## Status

Accepted

## Context

`GET /api/me` is the first endpoint this service has beyond a health check, and
tier 2 records two things as undecided with an explicit deadline:

> **Error envelope and pagination:** *not yet defined.* Decide both before the
> second endpoint exists, not the tenth — the Next.js client will encode
> whatever shape ships first.

That deadline is now. A client integrating against one endpoint absorbs its
shape into every error handler it writes; changing it later is a breaking change
disguised as a refactor, and the migration package says the same about
pagination in stronger terms — *"cursor pagination on every list endpoint;
retrofitting is a breaking API change."*

A third question arrived by a different route. `users.email` was `citext` so that
comparison would be case-insensitive at every call site. Reviewing that surfaced
the real question: **where does normalisation belong?** Both writers already
lowercase — the ETL transform before building a row, and the API schema before a
handler runs — so a case-insensitive column type was a second mechanism for an
invariant already held, and the only object in the chain whose behaviour on
Supabase was unverified.

## Decision

**1. Every failure is RFC 9457 Problem Details**, served as
`application/problem+json`:

```json
{"type": "about:blank", "title": "Not Found", "status": 404, "detail": "..."}
```

A published standard rather than a bespoke envelope. Clients get one shape;
`api/errors.py` holds the single mapping from `AppError` subclasses to status
codes, and **`domain/` never names an HTTP status** — which is what lets a rule
be unit-tested without a request and reused by the ETL, which has no request.

Two consequences worth stating because they are easy to undo:

- **`ConfigurationError` maps to 500 with its detail withheld.** It names
  settings, and "EDUFURTHER_SUPABASE_JWKS_URL is unset" tells an anonymous
  caller what this service is wired to.
- **A 401 carries no detail and no varying title.** Every authentication failure
  reads identically, because distinguishing "malformed" from "wrong signature"
  tells a prober which half of the guess was right.

**2. Cursor pagination on every list endpoint**, from the first one. `?cursor=`
and `?limit=`, returning `{"data": [...], "next_cursor": ...}`. Ids are UUIDv7
and therefore time-ordered (ADR 0015), so the id *is* the keyset cursor — no
offset, no separate sort column, and no page drift when a row is inserted
mid-scroll. Decided here rather than at the first list endpoint so that endpoint
inherits it instead of choosing.

**3. Normalisation happens at the boundary, and the database constrains rather
than transforms.** Every writer normalises on the way in: the API schema in
`api/schemas/`, the ETL in `domain/transform.py`. The column then carries a
`CHECK` asserting what the boundary already guarantees.

`users.email` is therefore `text` with `CHECK (email = lower(email))`, and the
`citext` extension is dropped. **The CHECK is not redundant with the boundary** —
it is what fails loudly when a future writer forgets, which is precisely what
`citext` was insuring against, except a constraint refuses where a
case-insensitive type would have silently stored a second spelling nobody could
find again.

Normalisation is declarative — a Pydantic validator on a shared mixin — so a new
route cannot omit it by not thinking about it.

**4. Every route documents itself in OpenAPI.** A `summary`, a `description`, and
its failure modes; tags carry descriptions of the group. `/docs` is the contract
the client is built against, and a reader should not have to open a route module
to learn how a call can fail.

**5. Routers ship with the module that owns them**, from M2 onward. Migrating six
phases and then adding routing means the schema is never exercised by real usage
until it is expensive to change.

**6. The version is in the path, from the first endpoint: `/api/v1/...`.** Not
because a v2 is foreseen — it is not — but because the cost is asymmetric. A
version segment added now is one string in one router. Added after the Next.js
client ships, it is either a breaking change for that client or an unversioned
alias maintained indefinitely, and `project-conventions` already records that the
client "will encode whatever shape ships first".

Header versioning was not considered seriously: it is harder to debug, cache and
log, and a path segment is legible in an access log without tooling.

**`/health` stays unversioned.** It is read by the platform's health check, not
by a client, and it is not part of the contract a v2 would fork. Moving it under
`/api/v1/` would tie an operational probe to a client-facing version number and
break the probe on the day that number changes.

**A v2 is a fork of `api/schemas/` only.** Both versions map to the same domain
services; the moment `domain/` learns which version called it, the layer boundary
this record relies on for testability is gone.

### Rejected alternatives

**FastAPI's default `{"detail": "..."}`.** Free, and already what unhandled
errors produce. Rejected because it carries no status in the body, no stable
machine-readable type, and no room to extend without inventing a convention —
which is how a bespoke envelope gets built one field at a time.

**Offset pagination.** Simpler to write and to reason about for page 3 of 10.
Rejected on the package's own grounds and on correctness: an offset page shifts
when a row is inserted above it, so a client paging through a list can see a row
twice or never.

**Keeping `citext`.** It genuinely makes `WHERE email = :x` correct without the
caller lowering. Rejected because both writers already normalise, so it insures
against a case that cannot arise without a *third* writer — and that writer is
better served by a constraint that refuses than a type that accepts. It also
generated a Supabase advisory (`0014_extension_in_public`) whose stated risk,
PostgREST exposure, ADR 0005 had already removed. A warning we could correctly
ignore is worse than no warning.

## Consequences

**The client can be written against one error shape**, and the second endpoint
inherits the first's contract rather than negotiating a new one.

**`domain/` stays free of HTTP.** The mapping table in `api/errors.py` is the
only place a status code appears, and an `AppError` subclass nobody mapped
returns 500 rather than a guessed 4xx — an unmapped error is a gap in that table
and reporting it as the caller's fault would hide it.

**Dropping `citext` removed the last unverified object in the chain.** What
remained was a question `alembic check` could not answer without a real Supabase
project; there is now nothing to answer.

**Pagination is decided before anything needs it**, which is the only time the
decision is free.

### Confirmation

- **Mechanical:** every failure path of `GET /api/me` asserts
  `application/problem+json`, and six separate tests assert that an absent,
  unsigned, wrongly-signed, expired, wrongly-audienced or malformed token is
  **refused**. An auth check is only worth testing in that direction.
- **Mechanical:** a test asserts every rejection returns an identical body, so a
  future handler cannot start explaining which check failed.
- **Mechanical:** `CHECK (email = lower(email))` is asserted by an integration
  test in both directions — a mixed-case insert is rejected, a normalised one is
  stored and found.
- **Not mechanical:** nothing enforces that a *new* route sets `summary`,
  `description` and its failure responses, or that a new list endpoint uses a
  cursor. Both are review against this record. A test walking the OpenAPI schema
  for missing summaries would close the first half and does not exist.
- **Not mechanical:** nothing checks that a new `AppError` subclass joins the
  mapping table. It fails safe — 500 rather than a wrong 4xx — but it fails.

### Open questions

- **Whether `next_cursor` should be opaque.** Returning a raw UUIDv7 exposes an
  approximate creation time. Harmless for a user's own list; worth revisiting
  before any endpoint pages over other people's rows.
- **Rate limiting and its error shape.** A 429 belongs in the mapping table with
  the rest; nothing produces one yet.
