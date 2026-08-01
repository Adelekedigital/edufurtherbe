---
name: security-checker
description: Security review and hardening for Python backend services, anchored to OWASP ASVS and the API Security Top 10 — authn/authz, input validation, injection, secrets, dependencies, and data egress. Use before merging anything touching auth, user input, file uploads, serialization, database queries, outbound requests, or PII; when adding a dependency; when writing a new endpoint; or when the user asks for a security review or audit.
---

# Security Checker

Anchored to **OWASP ASVS v5** and the **OWASP API Security Top 10**. Run the
automated gate first, then the manual review — tools catch roughly the injection
and dependency classes; they catch almost none of the authorization ones, which
are the most commonly exploited.

## Automated gate

```bash
uv run bandit -q -ll -r src         # Python SAST, medium+ severity
uv run pip-audit                    # known CVEs in the dependency tree
gitleaks detect --no-banner         # secrets in history
uv run ruff check --select S .      # flake8-bandit rules inline
```

All must be clean before review. A finding is resolved by fixing it, or by a
`# nosec B###: <specific justification>` comment — never by deleting the check
or dropping the severity flag.

## Manual review checklist

Work top to bottom. The first two sections are where real breaches come from.

### 1. Broken object-level authorization (API1 — the #1 cause of real breaches)

The single most important question in any backend review:

> **For every endpoint that accepts an ID, is ownership of that ID verified
> against the authenticated principal?**

```python
# ❌ Authenticated, but not authorized. Any logged-in user reads any order.
@router.get("/orders/{order_id}")
async def get_order(order_id: str, user: CurrentUser, svc: OrderSvc):
    return await svc.get(order_id)

# ✅ Ownership is part of the query, not an afterthought.
@router.get("/orders/{order_id}")
async def get_order(order_id: str, user: CurrentUser, svc: OrderSvc):
    order = await svc.get_for_customer(order_id, customer_id=user.customer_id)
    if order is None:
        raise OrderNotFound(order_id)   # 404, not 403 — do not confirm existence
    return OrderRead.model_validate(order)
```

- Scope ownership **in the repository query**, not with a post-fetch `if`. A
  post-fetch check is one early-return away from being bypassed, and it leaks
  the row into logs and traces in the meantime.
- Return **404, not 403**, for objects the caller may not see. 403 confirms the
  object exists and turns an authorization bug into an enumeration oracle.
- Sequential integer IDs make enumeration trivial. Prefer UUIDv7 or prefixed
  ULIDs for anything externally addressable.

**Scope every path, not just the list.** Enumerate every read *and every write*
against the entity and confirm each applies the ownership filter:

```sh
grep -rn "select(Order)\|update(Order)\|delete(Order)" src/ --include=*.py
```

A filtered list endpoint with an unscoped `POST /orders/{id}/cancel` is not
protected — it is protected against browsing. **A hidden row is not a protected
row.** Attackers do not need your list endpoint to obtain an id.

**Use one shared predicate.** Put the filter in a single function every path
calls, rather than hand-rolling the `WHERE` at each call site. A predicate a
caller can forget is a predicate a caller will forget — and one function means a
fix lands everywhere at once.

**Derive the caller's scope; never accept it from the request.** Resolve the
owner from the verified token, or from the row being acted on. A client-supplied
`customer_id` or `org_id` is not an authorization boundary; it is a request for
one. Where an endpoint legitimately lets a caller name a scope — an admin acting
on behalf of someone — cross-validate it against something the caller does not
control.

**Reject explicitly when the scope resolver returns nothing.** If `None` means
"unscoped" anywhere downstream, then a user who belongs to nothing sees
everything:

```python
scope = resolve_scope(user)
if scope is None and not user.is_admin:
    raise Forbidden()          # never fall through to a downstream branch
```

**Translate external identifiers once, at the edge.** Comparing a session's
external id against a column holding an internal one never matches, raises
nothing when both are UUIDs, and fails *closed* — so it presents as a
permissions bug and gets debugged in the permissions code, where the defect is
not. See `persistence-patterns`.

If tenants in your system are separate customer organisations that must never see
each other's data, use the `tenant-isolation` skill, which goes considerably
deeper than this section.

### 2. Authentication and session

- [ ] Passwords hashed with **argon2id** (or bcrypt cost ≥ 12). Never SHA-*, MD5,
      or anything unsalted.
- [ ] JWTs: algorithm **pinned server-side** (never read `alg` from the token —
      that is the `alg: none` / HS-vs-RS confusion attack), `exp`/`iat`/`aud`/
      `iss` all validated, short TTL, refresh tokens rotated and revocable.
- [ ] Token comparison via `secrets.compare_digest`, never `==` (timing).
- [ ] Rate limiting on login, password reset, and token endpoints, keyed by both
      account and source IP.
- [ ] Generic failure messages — "invalid credentials", never "no such user".
- [ ] Logout / password change invalidates existing sessions server-side.

### 3. Input validation

- [ ] Every request body, query param, and header is a **Pydantic model** with
      constrained types (`StrictStr`, `conint(ge=…)`, `max_length`, patterns).
      Validation at the edge, in `api/schemas/`, before the domain sees anything.
- [ ] `model_config = ConfigDict(extra="forbid")` on request models — silently
      ignored unknown fields hide client bugs and mass-assignment attempts.
- [ ] Body size limits enforced at the proxy *and* the app.
- [ ] Pagination has a **hard max** page size, not just a default.
- [ ] Never bind a request body directly to an ORM model (mass assignment).

### 4. Injection

- [ ] **SQL**: parameterized queries or the SQLAlchemy expression API only.
      Never f-strings or `%` into SQL — including into `ORDER BY` and table
      names, which is where it usually sneaks in. Validate sort fields against an
      explicit allowlist.
- [ ] **Command**: avoid `subprocess` with `shell=True`. Pass an argument list.
- [ ] **Deserialization**: never `pickle`, `yaml.load`, or `eval` on untrusted
      input. Use `json` and `yaml.safe_load`.
- [ ] **Path traversal**: resolve and confirm containment before any file access.

```python
base = Path("/srv/uploads").resolve()
target = (base / user_supplied).resolve()
if not target.is_relative_to(base):
    raise InvalidPath(user_supplied)
```

- [ ] **SSRF**: outbound URLs from user input must be allowlisted by host, with
      redirects disabled and link-local/private ranges (169.254.0.0/16, 10/8,
      127/8, ::1) blocked *after* DNS resolution.

### 5. Secrets and configuration

- [ ] Zero secrets in source, fixtures, test files, or committed `.env`.
      `.env.example` carries names with placeholder values only.
- [ ] All config through `core/config.py` (pydantic-settings). Secret values
      typed `SecretStr` so they cannot be logged by accident.
- [ ] `DEBUG` off in production; tracebacks never returned to clients.
- [ ] Credentials rotatable without a code change.

### 6. Data exposure

- [ ] Response models are **explicit allowlists** of fields. Never
      `model_validate(orm_object)` into a response that has extra attributes —
      `password_hash` has shipped that way more than once.
- [ ] PII redacted in logs and traces (see the `observability` skill).
- [ ] Error responses carry a correlation ID, not internal detail.
- [ ] No stack traces, SQL, or hostnames in 5xx bodies.

### 7. Dependencies and supply chain

- [ ] `uv.lock` committed; CI installs with `--frozen`.
- [ ] `pip-audit` clean; Dependabot enabled.
- [ ] New dependency justified in the PR body: what it does, why stdlib is
      insufficient, maintenance status, transitive weight.
- [ ] Pinned GitHub Actions by commit SHA, not by moving tag.

### 8. Transport and headers

- [ ] HTTPS enforced; HSTS set at the edge.
- [ ] **CORS is not `allow_origins=["*"]` with credentials** — that combination
      is invalid and, where browsers permit it, catastrophic. Enumerate origins.
- [ ] Security headers: `X-Content-Type-Options: nosniff`,
      `Content-Security-Policy`, `Referrer-Policy`, `X-Frame-Options`.
- [ ] `TrustedHostMiddleware` configured (Host header injection).

## Threat-model prompts for new endpoints

For each new route, answer in the PR body:

1. Who is allowed to call this, and where is that enforced?
2. What is the worst thing a valid-but-malicious caller can do with it?
3. What does it read or write that belongs to someone else?
4. What happens if it is called 10,000 times per second?
5. What does it log, and would that log entry be safe in a support ticket?

## Severity and response

| Severity | Examples | Action |
|---|---|---|
| **Critical** | RCE, authn bypass, secret in repo, SQLi | Block merge. Rotate any exposed credential *before* anything else. |
| **High** | Broken object-level authz, stored XSS, SSRF | Block merge. |
| **Medium** | Missing rate limit, verbose errors, weak headers | Fix in this PR or file a ticket with a date. |
| **Low** | Hardening, defense-in-depth | Backlog. |

A committed secret is compromised the moment it is pushed. Rewriting history
does not un-compromise it — **rotate first, then clean the history.**

`references/asvs-checklist.md` has the extended ASVS-mapped checklist for
audit-grade reviews.
