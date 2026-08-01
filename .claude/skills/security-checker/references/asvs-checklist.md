# Extended ASVS-Mapped Checklist

For audit-grade reviews, compliance evidence, or when a customer security
questionnaire is involved. Chapter numbers reference OWASP ASVS v5.

## V1 — Architecture & Threat Modeling

- [ ] A current data-flow diagram exists in `docs/architecture.md`
- [ ] Trust boundaries are explicit (edge, service, datastore, third party)
- [ ] Every component's authentication mechanism is documented
- [ ] Threat model reviewed when a trust boundary changes
- [ ] Security-relevant decisions captured as ADRs

## V2 — Authentication

- [ ] Passwords ≥ 12 chars, checked against a breached-password list
- [ ] No composition rules that force predictable patterns
- [ ] No password hints, no security questions
- [ ] argon2id (m=19MiB, t=2, p=1 minimum) or bcrypt cost ≥ 12
- [ ] Credential stuffing defense: rate limit + progressive delay + MFA offer
- [ ] MFA available; enforced for administrative roles
- [ ] Password reset tokens: single-use, ≤ 30 min TTL, invalidated on use
- [ ] Account enumeration impossible via login, reset, or registration timing
- [ ] Service-to-service auth uses mTLS or signed short-lived tokens

## V3 — Session Management

- [ ] Session tokens ≥ 128 bits from a CSPRNG (`secrets`, not `random`)
- [ ] New session ID issued on privilege change
- [ ] Idle and absolute timeouts both enforced
- [ ] Cookies: `Secure`, `HttpOnly`, `SameSite=Lax` or `Strict`, `__Host-` prefix
- [ ] Server-side revocation works (a stateless JWT alone does not satisfy this)
- [ ] Concurrent session limit / visibility for sensitive accounts

## V4 — Access Control

- [ ] Deny by default; every route explicitly declares its authorization
- [ ] Object-level authorization enforced **in the query**, not post-fetch
- [ ] Function-level authorization enforced server-side, never by hiding UI
- [ ] Field-level: privileged fields cannot be set by unprivileged callers
- [ ] Multi-tenant: tenant ID is part of every query predicate; verified by test
- [ ] Administrative endpoints network-restricted in addition to authenticated
- [ ] Access control decisions logged with actor, object, action, outcome

## V5 — Validation, Sanitization, Encoding

- [ ] Positive (allowlist) validation on every external input
- [ ] Structured types over strings wherever a type exists
- [ ] Output encoding is contextual (HTML, JS, URL, SQL) at the point of use
- [ ] Templating auto-escaping enabled and not disabled per-call
- [ ] File uploads: content-type sniffed not trusted, extension allowlisted,
      size capped, stored outside the web root, served from a separate origin
- [ ] Archive extraction guarded against zip-slip and zip-bomb
- [ ] XML parsing has external entity resolution disabled (`defusedxml`)
- [ ] Regexes reviewed for catastrophic backtracking (ReDoS) on user input

## V6 — Cryptography

- [ ] `secrets` module for all tokens; never `random`
- [ ] AES-GCM or ChaCha20-Poly1305 for symmetric encryption; never ECB
- [ ] Nonces/IVs unique per encryption operation
- [ ] Keys from a KMS/secret manager, never derived from a config string
- [ ] Key rotation procedure documented and exercised
- [ ] No custom cryptographic constructions
- [ ] Hashing for integrity: SHA-256+. Never MD5 or SHA-1.

## V7 — Error Handling & Logging

- [ ] Generic error responses; detail only in server-side logs
- [ ] Logs are structured JSON with a correlation ID
- [ ] PII, credentials, tokens, and card data never logged (redaction tested)
- [ ] Security events logged: authn success/failure, authz denial, privilege
      change, data export, configuration change
- [ ] Logs shipped off-host; tamper-evident retention
- [ ] Log injection prevented (no raw newlines from user input)

## V8 — Data Protection

- [ ] PII inventoried; retention period defined and enforced by a job
- [ ] Encryption at rest for datastores and backups
- [ ] TLS 1.2+ in transit, internally as well as at the edge
- [ ] `Cache-Control: no-store` on responses containing sensitive data
- [ ] Deletion actually deletes (including backups, caches, and search indexes)
- [ ] Data export and erasure paths exist for GDPR/CCPA requests

## V9 — Communications

- [ ] TLS certificate validation never disabled (`verify=False` is a blocker)
- [ ] Outbound connections have connect *and* read timeouts set
- [ ] Retries are bounded and idempotency-safe
- [ ] Circuit breaker on third-party dependencies

## V10 — Malicious Code

- [ ] Dependencies pinned by hash in the lockfile
- [ ] CI runs on an untrusted-input-safe runner for fork PRs
- [ ] No `curl | bash` in build scripts
- [ ] Actions pinned by commit SHA

## V11 — Business Logic

- [ ] Rate limits reflect realistic human/system usage per endpoint
- [ ] Sequential workflows cannot be entered mid-sequence
- [ ] Time-of-check/time-of-use races guarded by DB constraints or locks
- [ ] Monetary and inventory operations are transactional and idempotent
- [ ] Anti-automation on high-value actions

## V12 — Files & Resources

- [ ] Path traversal blocked by resolve + containment check
- [ ] User-supplied filenames never used directly on disk
- [ ] Uploaded content served with `Content-Disposition: attachment` where
      rendering is not required

## V13 — API & Web Service

- [ ] OpenAPI schema is generated from code and kept current
- [ ] Unused HTTP methods rejected
- [ ] Content-Type enforced; JSON endpoints reject `text/plain` smuggling
- [ ] GraphQL (if present): depth and complexity limits, introspection off in
      production, field-level authorization
- [ ] Webhooks: signature verified, timestamp checked for replay, constant-time
      comparison

## V14 — Configuration

- [ ] Build reproducible from a committed lockfile
- [ ] Debug and dev endpoints absent from production images
- [ ] Containers run as non-root, read-only root filesystem
- [ ] Least-privilege IAM; no wildcard resource policies
- [ ] Security headers verified by an automated test, not by inspection
- [ ] Dependency and base-image scanning in CI, failing on Critical/High
