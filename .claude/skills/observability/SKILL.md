---
name: observability
description: Structured logging, metrics, and distributed tracing standards for Python services — structlog JSON logs, correlation IDs, RED/USE metrics, OpenTelemetry spans, health checks, and alerting on SLOs. Use when adding logging or metrics, instrumenting a new endpoint or background job, debugging a production incident, setting up alerts, or when asked why something cannot be diagnosed from the logs.
---

# Observability

Three signals, one job: answer "what is broken, for whom, and since when" in
under five minutes, without shipping new code.

Instrumentation is written **with** the feature, in the same PR. Added later it
is always wrong, because by then you have forgotten which branches mattered.

## 1. Structured logging

JSON to stdout. Never printf. Never f-strings into the message.

```python
# observability/logging.py
import logging
import structlog
from structlog.contextvars import merge_contextvars

def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    processors = [
        merge_contextvars,                       # correlation IDs, automatically
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        redact_sensitive,                        # see below — not optional
    ]
    processors.append(
        structlog.processors.JSONRenderer() if json_logs
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level]
        ),
        cache_logger_on_first_use=True,
    )
```

**The event name is a stable identifier; the detail goes in fields.**

```python
log = structlog.get_logger()

# ❌ Unsearchable, unaggregatable, and it leaks the email into the message.
log.info(f"Order {order.id} failed for {user.email}: {exc}")

# ✅ One event name, structured dimensions, redacted subject.
log.warning(
    "order.payment_failed",
    order_id=order.id,
    customer_id=user.id,          # ID, not email
    amount_cents=order.total_cents,
    provider="stripe",
    error_code=exc.code,
    retryable=exc.retryable,
)
```

You cannot build `count by error_code` from an f-string. You can from fields.

### Levels

| Level | Use for | Wakes someone? |
|---|---|---|
| `DEBUG` | Local development detail. Off in production. | No |
| `INFO` | Business events worth counting: created, shipped, refunded. | No |
| `WARNING` | Degraded but handled: retry exhausted, fallback taken. | No |
| `ERROR` | A request failed and a human should eventually look. | Ticket |
| `CRITICAL` | The service cannot serve. | Page |

If everything is `ERROR`, nothing is. A 400 from a malformed client request is
`INFO`, not `ERROR` — it is the system working correctly.

### Redaction is mandatory

```python
SENSITIVE_KEYS = frozenset({
    "password", "token", "secret", "authorization", "api_key", "cookie",
    "ssn", "card_number", "cvv", "email", "phone", "address", "access_token",
    "refresh_token", "private_key",
})

def redact_sensitive(_logger, _name, event_dict):
    for key in list(event_dict):
        if key.lower() in SENSITIVE_KEYS or "password" in key.lower():
            event_dict[key] = "[REDACTED]"
    return event_dict
```

Add a unit test asserting a log call with `password=` renders `[REDACTED]`.
Redaction that is not tested is redaction that silently stops working.

## 2. Correlation IDs

Every log line, metric exemplar, and span for one request carries the same ID,
so one grep reconstructs the whole request across services.

```python
# api/middleware.py
class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        cid = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        structlog.contextvars.bind_contextvars(
            request_id=cid,
            path=request.url.path,
            method=request.method,
        )
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()
        response.headers["X-Request-ID"] = cid
        return response
```

Rules:
- Accept an inbound `X-Request-ID`; generate one when absent.
- **Propagate it on every outbound call** — a correlation ID that stops at your
  service boundary solves a third of the problem.
- Return it in the response, and include it in every error body. Support tickets
  then arrive with the exact ID to search for.

## 3. Metrics

**RED for request-driven work, USE for resources.**

| Pattern | Signals |
|---|---|
| **RED** (services) | **R**ate, **E**rrors, **D**uration |
| **USE** (resources) | **U**tilization, **S**aturation, **E**rrors |

```python
# observability/metrics.py
from prometheus_client import Counter, Histogram, Gauge

REQUESTS = Counter(
    "http_requests_total", "HTTP requests",
    ["method", "route", "status"],
)
LATENCY = Histogram(
    "http_request_duration_seconds", "Request latency",
    ["method", "route"],
    buckets=(.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10),
)
IN_FLIGHT = Gauge("http_requests_in_flight", "Concurrent requests")

ORDERS_CREATED = Counter("orders_created_total", "Orders created", ["channel"])
```

**Cardinality discipline — this is how metrics backends get taken down.**
Labels must be bounded sets: route *templates* (`/orders/{id}`), not paths
(`/orders/ord_abc123`). Never label with a user ID, order ID, email, raw URL, or
free-text error message. A label with unbounded values creates one time series
per value.

Use `route.path` from the matched Starlette route, never `request.url.path`.

Record **business** metrics too, not just technical ones. `orders_created_total`
dropping to zero is a far better alert than CPU being fine.

## 4. Tracing

OpenTelemetry, auto-instrumented at the edges, manual spans around meaningful
work.

```python
# observability/tracing.py
def configure_tracing(app, settings) -> None:
    provider = TracerProvider(resource=Resource.create({
        "service.name": settings.service_name,
        "service.version": settings.version,
        "deployment.environment": settings.environment,
    }))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)
    SQLAlchemyInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()
```

```python
tracer = trace.get_tracer(__name__)

async def cancel(self, order_id: str, reason: str) -> Order:
    with tracer.start_as_current_span("order.cancel") as span:
        span.set_attribute("order.id", order_id)     # attributes may be high-card
        span.set_attribute("cancel.reason", reason)
        ...
```

Span attributes may be high-cardinality — that is the point of tracing, and it is
the opposite of the metrics rule. Sample at 100% in staging; head-sample 1–10% in
production with **tail sampling that always keeps errors and slow requests**.

## 5. Health checks

Two endpoints, different meanings. Conflating them causes cascading restarts.

```python
@router.get("/healthz")       # LIVENESS: is the process wedged?
async def healthz() -> dict[str, str]:
    return {"status": "ok"}   # No dependency checks. Ever.

@router.get("/readyz")        # READINESS: can it serve traffic right now?
async def readyz(db: DbSession) -> dict[str, Any]:
    checks = {"database": await ping_db(db), "cache": await ping_cache()}
    ok = all(checks.values())
    return JSONResponse({"status": "ok" if ok else "degraded", "checks": checks},
                        status_code=200 if ok else 503)
```

If liveness checks the database, a brief DB blip restarts every pod
simultaneously, turning a recoverable dependency issue into a full outage.

## 6. Alerting

Alert on **symptoms users feel**, defined as SLO burn — not on causes.

- ✅ "p99 latency > 2s for 10 minutes" — users are waiting
- ✅ "5xx rate > 1% for 5 minutes" — users are failing
- ✅ "orders_created_total == 0 for 15 minutes during business hours"
- ❌ "CPU > 80%" — may be perfectly healthy; pages people for nothing

Every alert must be **actionable** and carry a runbook link. An alert nobody can
act on trains the team to ignore the channel, which is worse than having no alert.

## Instrumentation checklist for a new endpoint

- [ ] One `INFO` event on the business outcome, with IDs not PII
- [ ] `WARNING` on handled degradation; `ERROR` only on genuine failure
- [ ] Route registered with templated label in the metrics middleware
- [ ] A business counter if this endpoint represents a countable event
- [ ] A span around any external call or expensive computation
- [ ] Correlation ID propagated to every outbound request
- [ ] Redaction test covers any new sensitive field
- [ ] If it can fail in a user-visible way, an SLO and alert exist
