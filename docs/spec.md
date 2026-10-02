# Spec — Failed Payment Recovery Agent

## 1. Purpose

Build a payment-recovery system that receives Razorpay payment events, determines why a payment failed, decides what recovery action should happen, schedules that action, and sends recovery emails only when the payment is confirmed to require recovery.

The system must also track whether recovery succeeded and expose payment-health and recovery metrics through a dashboard.

This specification is based on the existing payment-recovery pipeline and the defined payment-failure actions. The implementation must not add behavior that is not specified here.

---

## 2. Current Setup / Preconditions

The following infrastructure and setup are already completed and must be treated as existing dependencies:

- Razorpay is configured in Test Mode.
- A Razorpay webhook has been created.
- ngrok is installed, authenticated, and already configured to expose the local FastAPI server.
- Redis Cloud is available as the Celery broker.
- PostgreSQL is hosted on Render and is the system database.
- Resend is selected as the email provider.
- The application uses environment variables for credentials and configuration.

The current local webhook flow is:

```text
Razorpay
   ↓ HTTPS webhook
ngrok
   ↓
FastAPI on localhost:8000
```

The FastAPI webhook endpoint is:

```text
POST /webhook/razorpay
```

The public ngrok URL must be the URL already configured in the Razorpay webhook, followed by:

```text
/webhook/razorpay
```

Do not redesign the ngrok/webhook setup as part of this implementation.

---

## 3. Core Objective

For every incoming Razorpay payment event:

1. Validate that the webhook is genuine.
2. Ensure the same webhook event is processed only once.
3. Persist the event and payment information.
4. Classify failed payments into a defined failure category.
5. Create the appropriate recovery action schedule.
6. Process due actions asynchronously.
7. Re-check the latest Razorpay payment status before taking a recovery action.
8. Send recovery communication through email only.
9. Track recovery outcomes.
10. Keep PostgreSQL as the source of truth for state and scheduling.
11. Surface payment-health and recovery information through the dashboard.

---

## 4. Required Asynchronous Behaviour

Asynchronous processing is required and is supported by the current architecture.

The webhook request must remain fast and must **not** wait for slow external work such as:

- sending email,
- creating/fetching recovery links,
- long-running recovery processing,
- other external API calls.

The webhook should:

```text
Receive event
    ↓
Verify signature
    ↓
Deduplicate event
    ↓
Persist event/payment state
    ↓
Create scheduled action(s)
    ↓
Return HTTP 200
```

The actual recovery work must happen asynchronously after the webhook response.

The existing design uses:

```text
PostgreSQL
   ↓
scheduled action becomes due
   ↓
Celery Beat checks every minute
   ↓
Celery task is queued
   ↓
Redis acts as broker
   ↓
Celery worker executes action
```

PostgreSQL must remain the source of truth. Redis is only the broker/task-transport layer. If queued Redis messages are lost, the scheduler must be able to rediscover due actions from PostgreSQL.

---

## 5. Failure Categories and Actions

Use only these defined categories:

| Failure type | What it means | Required action |
|---|---|---|
| **Insufficient funds** | The customer's account or card limit could not cover the payment | Send a recovery email containing a fresh payment link and suggest another payment method. Retry according to the configured delay policy. |
| **Bank declined** | The bank declined the payment; it may be a hard decline or temporary bank-side issue | **Hard decline:** send a recovery email with a fresh payment link and suggest another payment method; do not retry the same card. **Temporary:** wait 15–30 minutes, then send a recovery email with a fresh payment link. |
| **Timeout** | The payment did not complete in time and the customer's account may have been debited | Check the latest Razorpay status first. If still pending, wait and check again. If captured, mark paid. If debited but not captured, do not immediately request another payment. Send recovery email only after confirming the payment genuinely failed. |
| **User cancelled** | The customer closed or cancelled the payment flow | Do not treat it as a normal payment failure. At most one soft recovery email may be sent a few hours later, or nothing. |
| **Other** | Unknown or unmapped error | Log the raw error details, place the case into manual review, and send no automatic recovery email until classified. |

Customer recovery channel:

```text
Email only
```

Email provider:

```text
Resend
```

Do not implement SMS recovery.

Do not implement Slack recovery or notification actions.

---

## 6. Failure Classification Requirements

Use **ChatGroq** for payment-failure classification.

The classifier must use the Razorpay failure fields:

- `error_code`
- `error_reason`
- `error_source`
- `error_step`

The LLM must classify the payment into exactly one of the defined categories:

```text
insufficient_funds
bank_declined_soft
bank_declined_hard
timeout
user_cancelled
other
```

The classification output must be constrained to a validated structured schema.

### Structured output requirement

Use ChatGroq with LangChain's `with_structured_output()` using:

```python
method="json_schema"
```

and a supported model/configuration.

Do **not** rely on the default function-calling/tool-calling structured-output path for this classifier.

The classification schema should contain at minimum:

```python
category
confidence
reason
```

where:

- `category` is restricted to the six allowed categories.
- `confidence` is constrained to `0.0` through `1.0`.
- `reason` is a concise explanation based only on the supplied Razorpay error information.

The implementation must validate the returned structured object before using the category to determine recovery policy.

### Asynchronous classification

LLM classification must **not** execute inside the synchronous Razorpay webhook request.

The webhook should persist the event/payment information and enqueue a background classification task:

```text
Razorpay webhook
        ↓
signature verification
        ↓
deduplication
        ↓
persist webhook + payment
        ↓
queue classification task
        ↓
return HTTP 200
```

Then asynchronously:

```text
Celery worker
        ↓
ChatGroq classification
        ↓
structured-output validation
        ↓
failure category
        ↓
recovery policy
        ↓
scheduled_actions
```

The system must remain operational if classification temporarily fails. A classification failure should be represented explicitly and retried/rescheduled through the background-worker mechanism rather than silently assigning an invented category.

The system must preserve the raw Razorpay error fields and the classification result so the decision can be inspected later.

Do not invent Razorpay error-code mappings. The LLM must classify from the actual Razorpay error information supplied to it.

---

## 7. Recovery Policies

The starting policy is:

| Category | Action | Delay |
|---|---|---|
| `insufficient_funds` | `send_recovery_email` | 2m |
| `bank_declined_soft` | `send_recovery_email` | 20m, 2h |
| `bank_declined_hard` | `send_recovery_email` | 5m |
| `timeout` | `send_recovery_email` | 10m, only after status verification |
| `user_cancelled` | `send_email_reminder` | 3h, once |
| `other` | `manual_review` | none |

Guardrails:

- Maximum 3 messages per payment.
- No customer email between 21:00 and 09:00 IST; move the action to 09:00 IST.
- Overall recovery expiry is 7 days.
- A recovered payment must cancel remaining pending recovery actions.

These values must be configurable rather than hard-coded throughout business logic.

---

## 8. Razorpay Events

The system must handle these events:

### `payment.failed`

Required processing:

1. Persist the payment.
2. Preserve payment and customer information required for recovery.
3. Classify the failure.
4. Determine the applicable recovery policy.
5. Create the scheduled recovery action(s).
6. For eligible categories, support a holdout group so recovery impact can be measured later.

### `payment.captured`

Required processing:

1. Mark the payment as captured.
2. Cancel pending recovery actions for the captured payment.
3. Also cancel pending recovery actions belonging to earlier failed payments for the same `order_id`, because the customer may have retried successfully.
4. Mark an associated recovery attempt as recovered when the captured payment can be linked to a recovery action.

### `payment_link.paid`

Use this event to match a paid recovery link to its recovery attempt when the event and payload are confirmed against the current Razorpay documentation.

The exact event name and payload must be verified during implementation.

---

## 9. Webhook Security

The webhook endpoint must:

1. Read the raw HTTP request body.
2. Read `X-Razorpay-Signature`.
3. Generate the expected HMAC-SHA256 signature using `RAZORPAY_WEBHOOK_SECRET`.
4. Compare signatures using a constant-time comparison.
5. Reject invalid signatures.
6. Read the Razorpay event ID.
7. Deduplicate using the event ID before processing the event.

The webhook event ID must be unique in PostgreSQL.

A duplicate webhook must return successfully without creating duplicate payment processing or duplicate scheduled actions.

---

## 10. Idempotency

Every important operation must be safe to execute more than once.

Required guarantees:

- Duplicate Razorpay webhook → no duplicate processing.
- Duplicate scheduled action execution → no duplicate recovery email.
- Worker crash after partial processing → retry must not create another recovery message.
- Multiple workers → a single scheduled action must be claimed by only one worker at a time.
- Payment becomes captured while recovery is pending → recovery action is cancelled.
- Recovered payment → remaining recovery actions are cancelled.

The recovery attempt record must provide a unique link between an action and its recovery result.

---

## 11. Scheduling and Worker Behaviour

The scheduler must periodically find due actions from PostgreSQL.

The worker must not rely on stale payment state from the original webhook.

Immediately before sending a recovery email, it must fetch the latest Razorpay payment state.

Required decision sequence:

```text
Load scheduled action
        ↓
Fetch current Razorpay payment status
        ↓
Is payment already captured / order already paid?
        ├── Yes → cancel action
        └── No
             ↓
Is action still allowed?
             ├── No → cancel/reschedule according to policy
             └── Yes
                  ↓
Create recovery Payment Link
                  ↓
Send recovery email through Resend
                  ↓
Record recovery attempt
                  ↓
Mark action completed
```

For temporary external failures such as network failures, rate limits, or provider errors, the action must be rescheduled with backoff rather than being lost.

---

## 12. Recovery Email Requirements

Email is the **only customer-facing recovery action**.

The system must:

- Use Resend.
- Send a fresh recovery Payment Link.
- Keep customer-facing wording neutral and clear.
- For insufficient funds and hard bank declines, suggest using another payment method.
- Never send a recovery email when a payment has already succeeded.
- Record each recovery email attempt.
- Respect the message-count, quiet-hours, and expiry guardrails.

The webhook handler must not directly send the recovery email. Email delivery belongs to asynchronous worker processing.

---

## 13. Recovery Tracking

For every recovery action, record:

- Original payment ID.
- Action ID.
- Recovery link ID.
- Recovery link URL.
- Channel.
- Treatment/holdout group.
- Delay used.
- Sent timestamp.
- Recovery status.
- Recovered payment ID when available.
- Recovery timestamp when available.

Recovery attribution rules:

### Recovered

Payment succeeds within 48 hours after a recovery message was sent, through the recovery link or the same order.

### Self-recovered

Customer retries successfully without a recovery message having been sent.

### Holdout

Eligible failures assigned to the holdout group receive no recovery email so recovery rates can be compared later.

### Revenue recovered

Sum the payment amounts for attributed recoveries.

### Revenue at risk

Failed payments that have not yet recovered or expired.

---

## 14. Payment Health Monitoring

The system must maintain health snapshots at one-minute intervals.

Snapshots cover:

- Global payment health.
- Bank-level health.
- Payment-method health.

At minimum calculate:

```text
attempts
captured
failed
success_rate
```

Rules:

- Exclude `user_cancelled` from the failure denominator.
- Exclude pending payments from the denominator.
- Count payment attempts, not customers.
- Keep attempt counts with snapshots.

---

## 15. Health Alerts

Health alerts are a **dashboard-only capability**.

No Slack notification is required.

The alert engine must evaluate health every minute.

Trigger an alert when:

- Attempts meet the configured minimum volume.
- Success rate is below the configured absolute floor, **or**
- Success rate is more than the configured number of percentage points below the same-hour seven-day baseline.

Use alert-state hysteresis:

```text
ok
  ↓ condition true
alerting
  ↓ condition remains true
alerting
  ↓ condition false for 2 consecutive checks
ok
```

The dashboard alert feed must show:

- scope,
- current success rate,
- baseline,
- attempt count,
- alert state.

Repeated alerts should not create alert-state flapping.

---

## 16. Dashboard Requirements

Provide a React dashboard backed by read-only backend endpoints.

Required dashboard information:

1. Overall success rate.
2. Failed payment count.
3. Recovered payments.
4. Revenue recovered.
5. Success-rate time series.
6. Failure-reason distribution.
7. Payment health by bank.
8. Payment health by method.
9. Recovery funnel.
10. Treated vs holdout recovery comparison.
11. Dashboard-only health alert feed.

Required backend read-only endpoints:

```text
GET /api/stats/summary
GET /api/stats/timeseries?hours=24
GET /api/stats/failure-reasons?hours=24
GET /api/stats/by-bank?hours=1
GET /api/stats/by-method?hours=1
GET /api/recovery/funnel?hours=24
GET /api/alerts
```

The dashboard may poll periodically for updated data.

---

## 17. Persistence Requirements

PostgreSQL is the system of record.

The persistent model must support at least:

### Webhook events

- event ID
- event type
- payload
- received timestamp

### Payments

- payment ID
- order ID
- amount
- currency
- method
- bank
- status
- error fields
- failure category
- customer email/contact
- timestamps

### Scheduled actions

- action ID
- payment ID
- action type
- step
- run time
- status
- result
- attempts
- lock/lease data
- error details
- creation/completion timestamps

### Recovery attempts

- action ID
- original payment ID
- recovery link information
- channel
- group
- delay
- sent time
- recovery status
- recovered payment information

### Health snapshots

- timestamp
- scope
- scope value
- attempts
- captured
- failed
- success rate

### Alert state

- scope
- scope value
- state
- timestamps
- notification/alert state information
- healthy streak

All timestamps must be stored in UTC. Convert to Asia/Kolkata only when applying quiet-hour rules or displaying data.

---

## 18. Environment Contract

The application must read credentials/configuration from environment variables.

Required keys:

```env
RAZORPAY_KEY_ID=<Razorpay test API key>
RAZORPAY_KEY_SECRET=<Razorpay test API secret>

RAZORPAY_WEBHOOK_SECRET=<custom webhook secret>

DATABASE_URL=<Render External PostgreSQL connection URL>

REDIS_URL=<Redis Cloud rediss:// connection URI>

NGROK_AUTHTOKEN=<ngrok token>

EMAIL_API_KEY=<Resend API key>

GROQ_API_KEY=<Groq API key>
```

LangSmith environment variables are present for tracing/observability:

```env
LANGCHAIN_TRACING_V2=true
LANGCHAIN_ENDPOINT=https://api.smith.langchain.com
LANGCHAIN_API_KEY=<LangSmith API key>
LANGCHAIN_PROJECT=razorpay_failed_payment_recovery_agent
```

Do not place real secrets in source code or commit `.env` to version control.

The webhook secret is custom-created and must be the same value configured in Razorpay's webhook and `RAZORPAY_WEBHOOK_SECRET`.

---

## 19. Non-Goals

The project must not add the following unless explicitly requested later:

- RAG.
- AI-generated payment decisions outside the defined failure-classification task.
- SMS recovery.
- Slack recovery or Slack alerting.
- Unspecified payment channels.
- Unspecified Razorpay event types.
- Automatic invention of Razorpay error-code mappings.
- Replacing PostgreSQL with Redis as the source of truth.

The LLM is used only for the defined **failure-classification task**. Recovery timing, guardrails, scheduling, payment-status checks, and execution policies remain deterministic and policy-driven.

---

## 20. Acceptance Criteria

### Webhook

- A valid Razorpay webhook reaches `POST /webhook/razorpay`.
- Invalid signatures are rejected.
- Replaying the same event produces only one persisted webhook event.
- The endpoint returns quickly without waiting for email delivery or other slow external work.

### Payment processing

- `payment.failed` creates a persisted payment record and appropriate scheduled action(s).
- `payment.captured` cancels relevant pending recovery actions.
- Recovery links can be associated back to the original failed payment.

### Failure policy

- Each defined failure category maps to the correct action and delay policy.
- Unknown failure information falls back to `other`.
- No recovery email is sent for a payment confirmed as successful.

### Asynchronous execution

- Due actions are discovered from PostgreSQL.
- Actions are processed by background workers.
- A worker crash does not permanently lose an action.
- Multiple workers cannot process the same scheduled action simultaneously.

### Email recovery

- Resend is the only customer recovery channel.
- Recovery emails contain a valid recovery Payment Link.
- Duplicate execution of the same action does not send duplicate recovery emails.

### Recovery tracking

- Successful recovery is recorded.
- Remaining recovery actions are cancelled after recovery.
- Self-recovered and holdout cases can be separated from attributed recovery.

### Monitoring

- Health snapshots are generated every minute.
- Dashboard metrics reflect PostgreSQL state.
- Health alert state transitions work with the defined two-check hysteresis.

### Hardening tests

The implementation must support tests for:

1. Duplicate webhook delivery.
2. Worker crash after action claim.
3. Multiple workers processing due actions.
4. Payment captured while a recovery action is pending.
5. Timeout where Razorpay status is still pending/authorized.
6. Alert threshold hysteresis.
7. Invalid webhook signature.

---

## 21. Definition of Done

The system is complete when:

```text
Razorpay
   ↓
ngrok
   ↓
FastAPI webhook
   ↓
signature verification + deduplication
   ↓
PostgreSQL
   ↓
failure classification + scheduled actions
   ↓
Celery Beat
   ↓
Redis
   ↓
Celery Worker
   ↓
fresh Razorpay status check
   ↓
recovery Payment Link
   ↓
Resend email
   ↓
customer payment
   ↓
Razorpay webhook
   ↓
recovery attribution
   ↓
PostgreSQL
   ↓
dashboard
```

and the required acceptance criteria and hardening tests pass.

---

## 22. Implementation Constraint for Agentic Coding

Treat this file as the **behavioural contract**.

### Database access

Use **Psycopg (Psycopg 3)** as the Python PostgreSQL database driver.

Do not introduce SQLAlchemy/ORM as a project dependency. Database operations should use Psycopg with parameterized SQL and explicit PostgreSQL transactions where required by the workflow.

Before implementing any feature:

1. Preserve the specified event flow.
2. Preserve asynchronous webhook-to-worker separation.
3. Preserve asynchronous ChatGroq classification; do not perform the LLM call inside the webhook request.
4. Use ChatGroq structured output with `method="json_schema"` for the classification task; do not default to function-calling/tool-calling structured output for this path.
5. Preserve PostgreSQL as the source of truth.
6. Preserve email-only customer recovery.
7. Do not invent unspecified external API fields, Razorpay error mappings, or event payloads.
8. Anything marked for Razorpay verification must be checked against current Razorpay documentation before implementation.
9. Keep credentials out of source code.
10. Do not create additional project functionality outside this specification without an explicit requirement.
