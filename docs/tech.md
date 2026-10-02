# Tech — Failed Payment Recovery Agent

## 1. Document Role

This file defines the technical architecture, technology choices, interfaces, implementation constraints, and engineering conventions for the project.

`spec.md` is the **behavioral source of truth**. `tech.md` is the **technical source of truth** for how that behavior should be implemented.

When these files conflict:

```text
spec.md → business behaviour / acceptance criteria
tech.md  → implementation architecture / engineering conventions
current verified provider documentation → provider-specific API facts
```

Do not invent missing requirements.

---

## 2. System Summary

The system is an event-driven, asynchronous payment-recovery platform.

Primary flow:

```text
Razorpay
   ↓ HTTPS webhook
ngrok
   ↓
FastAPI
   ↓
PostgreSQL
   ↓
Celery task
   ↓
Redis Cloud
   ↓
Celery Worker
   ↓
ChatGroq classification
   ↓
Recovery policy
   ↓
scheduled action
   ↓
Celery Worker
   ↓
Razorpay Payment Link
   ↓
Resend
   ↓
Customer email
```

Recovery status returns through Razorpay webhooks and is persisted back into PostgreSQL.

The system is intentionally **not an LLM-driven autonomous decision-maker**. ChatGroq has one bounded responsibility: classify the failure reason into a predefined category. Recovery policy, scheduling, payment-state verification, idempotency, guardrails, and execution remain deterministic.

---

## 3. Technology Stack

### Backend

| Technology | Responsibility |
|---|---|
| Python | Primary backend language |
| FastAPI | HTTP API and Razorpay webhook endpoint |
| Uvicorn | ASGI server for FastAPI |
| Psycopg (Psycopg 3) | PostgreSQL database driver; parameterized SQL and transaction handling |
| Pydantic | Request, response, and LLM structured-output validation |

### Asynchronous Processing

| Technology | Responsibility |
|---|---|
| Celery | Background task execution |
| Celery Beat | Periodic scheduling / due-action polling |
| Redis Cloud | Celery broker / task transport |

Redis is **not** the system of record.

### Database

| Technology | Responsibility |
|---|---|
| PostgreSQL on Render | Source of truth for events, payments, schedules, recoveries, health snapshots, and alert state |

### Payment Provider

| Technology | Responsibility |
|---|---|
| Razorpay Test Mode | Payment events, payment status, Payment Links, webhook delivery |

### Public Webhook Access

| Technology | Responsibility |
|---|---|
| ngrok | Public HTTPS tunnel from Razorpay to local FastAPI during development |

The ngrok tunnel is an infrastructure/development dependency, not application business logic.

### LLM

| Technology | Responsibility |
|---|---|
| ChatGroq through LangChain | Failure classification only |

The classifier receives Razorpay failure fields and returns one of the predefined failure categories.

### Email

| Technology | Responsibility |
|---|---|
| Resend | Customer recovery email delivery |

Email is the only customer-facing recovery channel.

### Frontend

| Technology | Responsibility |
|---|---|
| React | Dashboard UI |
| Recharts | Dashboard charts |

### Observability

| Technology | Responsibility |
|---|---|
| LangSmith | LLM tracing / observability |
| Application logs | Backend, worker, webhook, and integration diagnostics |
| Dashboard alert feed | Payment-health state visibility |

---

## 4. High-Level Architecture

```text
                         ┌───────────────────┐
                         │     Razorpay      │
                         │    Test Mode      │
                         └─────────┬─────────┘
                                   │
                            HTTPS webhook
                                   │
                                   ▼
                         ┌───────────────────┐
                         │       ngrok       │
                         │ public HTTPS URL  │
                         └─────────┬─────────┘
                                   │
                                   ▼
                         ┌───────────────────┐
                         │      FastAPI      │
                         │ :8000             │
                         └─────────┬─────────┘
                                   │
                  verify → dedupe → persist
                                   │
                                   ▼
                         ┌───────────────────┐
                         │    PostgreSQL     │
                         │     Render        │
                         │  source of truth  │
                         └─────────┬─────────┘
                                   │
                              queue task
                                   │
                                   ▼
                         ┌───────────────────┐
                         │   Celery / Redis  │
                         │   async workers   │
                         └─────────┬─────────┘
                                   │
                     ┌─────────────┴─────────────┐
                     │                           │
                     ▼                           ▼
              ChatGroq classifier          scheduled action
                     │                           │
                     └─────────────┬─────────────┘
                                   ▼
                         recovery policy
                                   │
                                   ▼
                         fresh Razorpay status
                                   │
                                   ▼
                         Payment Link API
                                   │
                                   ▼
                              Resend Email
                                   │
                                   ▼
                               Customer
```

---

## 5. Backend Responsibilities

The FastAPI application owns:

- Application startup.
- Health endpoint.
- Razorpay webhook endpoint.
- Webhook signature validation.
- Event deduplication.
- Persistence of incoming events.
- Persistence of payment state.
- Enqueuing background work.
- Read-only dashboard APIs.

The FastAPI request path must stay lightweight.

Do not perform these operations directly inside the webhook request:

- ChatGroq API call.
- Resend API call.
- Payment Link creation.
- Long-running processing.
- Recovery scheduling through an external queue without persistent PostgreSQL state.

---

## 6. API Surface

### Webhook

```text
POST /webhook/razorpay
```

Responsibilities:

```text
raw request
→ signature verification
→ event ID extraction
→ duplicate check
→ event/payment persistence
→ enqueue async classification
→ HTTP 200
```

### Health

```text
GET /health
```

Returns a minimal service-health response.

### Dashboard APIs

```text
GET /api/stats/summary
GET /api/stats/timeseries?hours=24
GET /api/stats/failure-reasons?hours=24
GET /api/stats/by-bank?hours=1
GET /api/stats/by-method?hours=1
GET /api/recovery/funnel?hours=24
GET /api/alerts
```

Dashboard endpoints are read-only.

---

## 7. Async Execution Model

Asynchronous behaviour is a required architectural feature.

### Webhook path

```text
Razorpay
   ↓
FastAPI
   ↓
validate
   ↓
persist
   ↓
enqueue task
   ↓
return 200
```

### Classification path

```text
Celery
   ↓
load persisted payment/error data
   ↓
ChatGroq
   ↓
validate structured result
   ↓
persist classification
   ↓
resolve policy
   ↓
create scheduled action(s)
```

### Recovery path

```text
Celery Beat
   ↓
find due PostgreSQL actions
   ↓
claim action safely
   ↓
worker
   ↓
fresh Razorpay status
   ↓
policy guards
   ↓
create Payment Link
   ↓
send email with Resend
   ↓
persist recovery attempt
```

### Periodic tasks

Celery Beat is responsible for periodic work such as:

```text
pick_due_actions
snapshot_health
evaluate_alerts
```

The schedule itself must live in PostgreSQL wherever the business action depends on durable timing.

Do not use Redis as the durable schedule.

---

## 8. ChatGroq Classification Architecture

ChatGroq is a bounded classification component.

Input:

```text
error_code
error_reason
error_source
error_step
```

Output:

```python
{
    "category": "...",
    "confidence": 0.0,
    "reason": "..."
}
```

Allowed categories:

```text
insufficient_funds
bank_declined_soft
bank_declined_hard
timeout
user_cancelled
other
```

### Structured output

Use LangChain ChatGroq structured output with:

```python
with_structured_output(
    FailureClassification,
    method="json_schema",
    strict=True,
)
```

Use a model/configuration that is verified to support the selected native JSON Schema structured-output path.

Do not depend on the default function-calling/tool-calling structured-output path for this classifier.

### Classification constraints

- Temperature should be deterministic/low.
- The prompt must provide the actual Razorpay error fields.
- The model must choose only from the allowed categories.
- The returned schema must be validated before policy execution.
- The model must not create new categories.
- The model must not decide message timing.
- The model must not decide whether a captured payment should be retried.
- The model must not execute tools.
- The model must not call Razorpay or Resend.
- The model must not generate SQL.

If classification fails, do not silently classify the payment as a valid category. Persist the classification failure state and retry through the asynchronous worker mechanism.

---

## 9. Deterministic Policy Layer

The LLM output is only an input to the deterministic policy engine.

```text
LLM classification
       ↓
validated category
       ↓
policy lookup
       ↓
action + delay + guardrails
```

Example:

```text
insufficient_funds
       ↓
send_recovery_email
       ↓
2 minutes
```

The policy engine owns:

- Action type.
- Delay.
- Quiet hours.
- Maximum messages.
- Expiry.
- Retry behaviour.
- Payment-status guards.
- Bank degradation checks.
- Cancellation rules.

Do not put these business rules inside the LLM prompt.

---

## 10. Failure Policies

Initial policies:

| Category | Action | Delay |
|---|---|---|
| `insufficient_funds` | `send_recovery_email` | 2m |
| `bank_declined_soft` | `send_recovery_email` | 20m, 2h |
| `bank_declined_hard` | `send_recovery_email` | 5m |
| `timeout` | `send_recovery_email` | 10m, only after status verification |
| `user_cancelled` | `send_email_reminder` | 3h, once |
| `other` | `manual_review` | none |

These values must be configuration-driven.

---

## 11. Data Ownership

### PostgreSQL

PostgreSQL owns durable state:

```text
webhook events
payments
scheduled actions
recovery attempts
health snapshots
alert state
classification result
classification failure state
```

### Redis

Redis owns only asynchronous task transport for Celery.

Do not use Redis as the source of truth for:

- Payment status.
- Recovery schedule.
- Customer recovery history.
- Webhook deduplication.
- Health metrics.

---

## 12. Idempotency and Concurrency

The implementation must use database-backed idempotency.

Required patterns include:

- Unique webhook event ID.
- Unique recovery attempt per action.
- Unique scheduled action per payment/action/step.
- PostgreSQL row locking.
- `FOR UPDATE SKIP LOCKED` or equivalent safe claiming mechanism.
- Lease/lock expiry for crashed workers.
- Re-check current payment state before customer-facing actions.

The system must be safe when:

- Razorpay repeats a webhook.
- Two workers see due work at nearly the same time.
- A worker crashes after claiming an action.
- A payment succeeds while a recovery action is waiting.
- A recovery email provider returns a temporary error.

---

## 13. Razorpay Integration Boundary

Create a dedicated Razorpay client/wrapper layer.

Application services should not scatter direct Razorpay SDK calls throughout unrelated modules.

The wrapper should provide focused operations such as:

```text
fetch_payment(...)
create_payment_link(...)
```

Webhook parsing should remain separate from outbound Razorpay operations.

Provider-specific fields must be verified against current Razorpay documentation before implementation when marked as unknown or provider-version dependent.

---

## 14. Resend Integration Boundary

Create a dedicated email/messaging layer.

Application services should call an abstraction such as:

```text
send_recovery_email(...)
```

The Resend-specific HTTP/SDK implementation belongs inside the messaging layer.

The email layer must not decide:

- Whether a payment should be recovered.
- How long to wait.
- Whether an action is safe.
- Whether the payment is captured.

Those decisions belong to policy/worker logic.

---

## 15. Database Design

### Database access technology

Use **Psycopg (Psycopg 3)** for all Python-to-PostgreSQL communication.

The project should use:

```text
Python application
      ↓
Psycopg 3
      ↓
PostgreSQL on Render
```

Use parameterized SQL for queries and mutations. Do not add an ORM layer.

Use explicit transactions for operations that must be atomic, especially webhook event deduplication and payment persistence.

Connection details come from `DATABASE_URL`.

The minimum persistent entities are:

```text
webhook_events
payments
scheduled_actions
recovery_attempts
health_snapshots
alert_state
```

The data model must support:

```text
event deduplication
payment history
failure classification
scheduled recovery
recovery attribution
health metrics
alert state
```

Use UTC internally.

Convert to `Asia/Kolkata` for:

- Quiet-hour decisions.
- User-facing time display.

---

## 16. Failure Handling

### Permanent/validation failure

Examples:

- Invalid webhook signature.
- Malformed required webhook payload.
- Invalid structured classification result.

Expected behaviour:

- Reject or mark failure explicitly.
- Log enough information for diagnosis.
- Do not silently continue with unsafe assumptions.

### Temporary external failure

Examples:

- Network failure.
- Rate limit.
- Provider 5xx.
- Temporary Redis/Celery issue.

Expected behaviour:

```text
failure
 ↓
record error
 ↓
reschedule/backoff
 ↓
retry asynchronously
```

Do not discard the scheduled business action.

---

## 17. Recovery Safety

Before sending any recovery email:

1. Load the scheduled action.
2. Fetch the latest Razorpay payment status.
3. Check whether the payment/order has already been paid.
4. Check action attempt limits.
5. Check quiet hours.
6. Check other configured policy guards.
7. Create the recovery Payment Link.
8. Send the email.
9. Persist the recovery attempt.
10. Mark the action completed.

A payment that has already succeeded must never receive a recovery email because of stale local state.

---

## 18. Security Requirements

### Secrets

Keep all secrets in environment variables.

Required credentials include:

```env
RAZORPAY_KEY_ID
RAZORPAY_KEY_SECRET
RAZORPAY_WEBHOOK_SECRET
DATABASE_URL
REDIS_URL
NGROK_AUTHTOKEN
EMAIL_API_KEY
GROQ_API_KEY
LANGCHAIN_API_KEY
```

Never hard-code these values.

Never commit `.env`.

### Webhook verification

Use:

```text
raw request body
+
RAZORPAY_WEBHOOK_SECRET
+
HMAC-SHA256
+
constant-time comparison
```

Do not parse/reformat the body before signature calculation.

### Customer data

Store only customer data required by the recovery flow and persistence model.

Do not log full credentials, secrets, or unnecessary sensitive payment information.

---

## 19. Observability

At minimum log structured information for:

### Webhook

```text
event_id
event_type
payment_id
signature_valid
duplicate
processing_result
```

### Classification

```text
payment_id
classification_status
category
confidence
latency
error
```

Do not log unnecessary customer-sensitive data.

### Recovery action

```text
action_id
payment_id
action_type
attempt
status
result
provider_error
```

### Health

Track:

```text
attempts
captured
failed
success_rate
```

Use LangSmith for ChatGroq tracing when enabled through the supplied environment configuration.

---

## 20. Testing Strategy

Tests should exist at several levels.

### Unit tests

Test:

- Failure classification schema validation.
- Policy selection.
- Delay calculation.
- Quiet-hour adjustment.
- Guardrails.
- Alert-state transitions.
- Recovery attribution logic.

### Integration tests

Test:

- Razorpay webhook signature verification.
- PostgreSQL persistence.
- Duplicate event handling.
- Celery task execution.
- Redis connectivity.
- Resend integration boundary.
- Razorpay integration boundary.

### Workflow tests

Required scenarios:

```text
duplicate webhook
worker crash
two workers
payment captured while recovery is pending
timeout while payment is still pending/authorized
classification failure
invalid webhook signature
alert hysteresis
```

### End-to-end demo

The intended flow is:

```text
Razorpay test event
→ ngrok
→ FastAPI
→ PostgreSQL
→ Celery
→ ChatGroq classification
→ scheduled action
→ Razorpay Payment Link
→ Resend email
→ customer pays
→ Razorpay webhook
→ recovery recorded
→ dashboard updated
```

---

## 21. Project Structure

Recommended structure:

```text
payment-recovery/
│
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── webhook.py
│   │   ├── events.py
│   │   ├── classifier.py
│   │   ├── policy.py
│   │   ├── db.py
│   │   ├── razorpay_client.py
│   │   ├── messaging.py
│   │   ├── stats.py
│   │   └── alerts.py
│   │
│   ├── worker/
│   │   ├── celery_app.py
│   │   └── tasks.py
│   │
│   ├── scripts/
│   │   ├── simulate_outage.py
│   │   └── seed_demo_data.py
│   │
│   ├── schema.sql
│   ├── requirements.txt
│   └── .env
│
├── frontend/
│
├── spec.md
├── tech.md
└── README.md
```

The project may adapt this structure when implementation needs it, but must keep responsibilities separated.

---

## 22. Dependency Principles

Prefer:

- Small, focused dependencies.
- Official provider SDKs where they simplify stable provider integration.
- Standard-library functionality when external dependencies are unnecessary.
- Pydantic for clear runtime data contracts.
- Psycopg (Psycopg 3) with parameterized SQL for safe PostgreSQL access.
- Explicit PostgreSQL transactions for state changes that must succeed or fail together.

Do not add a dependency solely for convenience when the existing stack can provide the required capability clearly.

Do not add an agent framework merely to label deterministic workflow logic as "agentic."

---

## 23. Configuration Principles

Business policy should be configurable.

At minimum, keep configurable:

- Recovery delays.
- Maximum recovery messages.
- Quiet hours.
- Recovery expiry.
- Health alert thresholds.
- Scheduler interval.
- Worker limits.
- Retry/backoff parameters.

Environment variables should contain secrets and environment-specific connection information.

Application code should contain business defaults, but policy values should not be duplicated across modules.

---

## 24. Agentic Coding Rules

This project is intended to be implemented through an agentic coding workflow.

The coding agent must:

1. Read `spec.md` before implementation.
2. Read `tech.md` before implementation.
3. Preserve the architecture and constraints defined here.
4. Make small, reviewable changes.
5. Avoid speculative features.
6. Avoid changing the external contract without an explicit requirement.
7. Verify provider-specific APIs from current documentation when the specification marks them as requiring verification.
8. Run relevant tests after meaningful changes.
9. Keep implementation and business policy separated.
10. Explain deviations from the specification before implementing them.

### Change discipline

For a requested change:

```text
Understand requirement
        ↓
Locate affected component
        ↓
Implement minimal change
        ↓
Run focused tests
        ↓
Run broader tests when appropriate
        ↓
Check configuration/contracts
        ↓
Report what changed
```

Do not rewrite unrelated modules during a focused change.

---

## 25. Source-of-Truth Rules for the Coding Agent

Use this priority order:

### Behaviour

`spec.md`

### Architecture / implementation conventions

`tech.md`

### Existing code

Current project implementation, provided it does not contradict the specification.

### External integrations

Current official provider documentation for:

- Razorpay
- Groq
- LangChain / LangChain-Groq
- Resend
- Celery
- Redis

When the provider documentation and an old example conflict, verify the current provider contract before implementation.

---

## 26. Explicit Technical Constraints

The implementation must preserve:

- Psycopg (Psycopg 3) for PostgreSQL access.
- Parameterized SQL instead of an ORM.

- FastAPI webhook endpoint: `POST /webhook/razorpay`.
- ngrok as the local public webhook tunnel.
- Render PostgreSQL as the durable application database.
- Redis Cloud as the Celery broker.
- Celery/Celery Beat for asynchronous processing.
- ChatGroq for failure classification.
- Native JSON Schema structured output for classification.
- No default function-calling/tool-calling structured output for classification.
- Resend as the only customer recovery channel.
- PostgreSQL as the source of truth.
- Dashboard-only health alerts.
- No Slack dependency.
- No SMS dependency.
- No RAG requirement.
- No LLM use outside the defined failure-classification responsibility.

---

## 27. Known Provider-Verification Areas

Do not guess these values.

Verify against current official documentation before implementation:

### Razorpay

- Webhook event names.
- Webhook payload shapes.
- Signature header.
- Event ID header.
- Payment status semantics.
- Payment Link API fields.
- Recovery-link webhook payload and linking mechanism.

### ChatGroq / Groq

- Current model support for native JSON Schema structured output.
- `with_structured_output()` compatibility.
- Current `method="json_schema"` behaviour.
- Whether `strict=True` is supported for the selected model.
- Current SDK/LangChain package compatibility.

### Redis / Celery

- TLS requirements for the Redis Cloud connection.
- Celery Redis SSL configuration.
- Current connection URL requirements.

### Resend

- Current API/SDK requirements.
- Sender verification requirements.
- Sending limits relevant to the deployment.

---

## 28. Non-Functional Requirements

### Reliability

- Duplicate events must not duplicate business effects.
- Worker crashes must not permanently lose scheduled actions.
- Successful payments must stop recovery processing.

### Security

- Webhooks must be authenticated.
- Secrets must remain outside source code.
- Database access must use credentials from environment variables.
- External API credentials must not appear in logs.

### Maintainability

- Separate integrations from business logic.
- Keep policies configurable.
- Use typed contracts for external/internal data.
- Keep modules focused.

### Testability

- Business rules must be testable without real Razorpay/Resend/Groq calls.
- External integrations should be mockable.
- Workflow tests should cover state transitions.

### Observability

- Failures must be diagnosable from logs and persisted state.
- LLM classification should be traceable through LangSmith when enabled.
- Dashboard metrics should derive from persistent state.

---

## 29. Definition of Technical Completion

Technical completion means:

```text
FastAPI
✓ runs locally on port 8000
✓ exposes /webhook/razorpay
✓ validates Razorpay webhook signatures
✓ deduplicates webhook events

PostgreSQL / Psycopg
✓ Psycopg 3 connects the application to PostgreSQL
✓ parameterized SQL is used for database operations
✓ stores durable payment/recovery state
✓ stores scheduled actions
✓ stores classification results

Celery + Redis
✓ execute background classification
✓ execute due recovery actions
✓ support safe action claiming and retries

ChatGroq
✓ classifies failure reason
✓ returns validated JSON Schema output
✓ runs asynchronously
✓ does not use tool-calling structured output for this path

Razorpay
✓ payment status can be re-checked
✓ recovery Payment Links can be created
✓ recovery events can be attributed

Resend
✓ sends recovery email only
✓ records send result

Dashboard
✓ shows health and recovery metrics
✓ shows dashboard-only alerts

Tests
✓ hardening scenarios pass
```

---

## 30. Final Engineering Principle

The architecture is intentionally:

```text
LLM for bounded interpretation
+
deterministic code for business policy
+
PostgreSQL for durable state
+
Celery/Redis for asynchronous execution
+
provider integrations behind clear boundaries
```

The LLM should interpret the failure; the application should make and execute the operational decision according to the specification.
