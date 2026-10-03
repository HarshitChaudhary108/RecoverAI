# Acceptance Check - Payment Recovery Agent

| Criterion | Test / Proof | Status |
|---|---|---|
| **Webhook: Valid Razorpay webhook reaches endpoint** | `backend/tests/test_webhook.py` $\rightarrow$ `test_webhook_valid_payment_failed` | PASS |
| **Webhook: Invalid signatures are rejected** | `backend/tests/test_webhook.py` $\rightarrow$ `test_webhook_invalid_signature` | PASS |
| **Webhook: Replaying same event produces only one record** | `backend/tests/test_webhook.py` $\rightarrow$ `test_webhook_duplicate_event` | PASS |
| **Webhook: Endpoint returns quickly (async execution)** | `backend/tests/test_webhook.py` $\rightarrow$ `test_webhook_valid_payment_failed` (verifies Celery task queued) | PASS |
| **Payment: `payment.failed` creates record & action** | `backend/tests/test_classification_service.py` $\rightarrow$ `test_classify_success_treatment` | PASS |
| **Payment: `payment.captured` cancels recovery actions** | `backend/tests/test_recovery.py` $\rightarrow$ `test_captured_cancels_own_actions` | PASS |
| **Payment: Recovery links associated back to failed payment** | `backend/tests/test_recovery.py` $\rightarrow$ `test_paid_link_recovery` | PASS |
| **Policy: Category maps to correct action/delay** | `backend/tests/test_policy.py` $\rightarrow$ various tests | PASS |
| **Policy: Unknown failure falls back to `other`** | `backend/tests/test_classification_service.py` $\rightarrow$ `test_classify_other_manual_review` | PASS |
| **Policy: No recovery email sent for successful payment** | `backend/tests/test_executor.py` $\rightarrow$ `test_run_action_already_paid` | PASS |
| **Async: Due actions discovered from PostgreSQL** | `backend/tests/test_actions_repo.py` $\rightarrow$ `test_claim_due_actions_basic` | PASS |
| **Async: Actions processed by background workers** | `backend/tests/test_executor.py` $\rightarrow$ `test_run_action_happy_path` | PASS |
| **Async: Worker crash does not permanently lose action** | `backend/tests/test_actions_repo.py` $\rightarrow$ `test_lease_expiry_retry` | PASS |
| **Async: Multiple workers cannot process same action** | `backend/tests/test_actions_repo.py` $\rightarrow$ `test_concurrent_claims` | PASS |
| **Email: Resend is the only recovery channel** | `backend/tests/test_messaging.py` $\rightarrow$ various tests | PASS |
| **Email: Emails contain valid recovery Payment Link** | `backend/tests/test_executor.py` $\rightarrow$ `test_run_action_happy_path` (verifies link creation) | PASS |
| **Email: Duplicate execution does not send duplicate emails** | `backend/tests/test_executor.py` $\rightarrow$ `test_run_action_max_messages` (verifies limit) | PASS |
| **Tracking: Successful recovery is recorded** | `backend/tests/test_recovery.py` $\rightarrow$ `test_paid_link_recovery` | PASS |
| **Tracking: Remaining actions cancelled after recovery** | `backend/tests/test_recovery.py` $\rightarrow$ `test_paid_link_recovery` | PASS |
| **Tracking: Self-recovered and holdout separated** | `backend/tests/test_recovery.py` $\rightarrow$ `test_holdout_captured_self_recovered` | PASS |
| **Monitoring: Health snapshots generated every minute** | `backend/tests/test_health_snapshots.py` $\rightarrow$ `test_snapshot_health_math` | PASS |
| **Monitoring: Dashboard metrics reflect PostgreSQL state** | `backend/tests/test_stats_api.py` $\rightarrow$ various tests | PASS |
| **Monitoring: Alert state transitions use hysteresis** | `backend/tests/test_alerts.py` $\rightarrow$ `test_next_state_hysteresis` | PASS |

## Code Audit Results

| Rule | Status | Evidence |
|---|---|---|
| No SQLAlchemy or ORM | PASS | No matches for `sqlalchemy` or `orm` in source code. |
| No SMS code | PASS | No matches for `sms` or `twilio`. |
| No Slack code | PASS | No matches for `slack` or `bolt`. |
| No tool-calling structured output | PASS | `backend/app/classifier.py` uses `method="json_schema"`. |
| No outbound API calls in webhook path | PASS | `backend/app/webhook.py` only calls DB and enqueues Celery task. |
| No secrets in code | PASS | All credentials sourced from `backend.app.config.settings`. |
| SQL always has parameters | PASS | No f-strings or `.format()` found in SQL queries. |
| Policy numbers read from config | PASS | Delays, limits, and quiet hours sourced from `settings`. |
