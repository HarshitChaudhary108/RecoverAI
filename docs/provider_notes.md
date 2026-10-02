# Provider Notes

## RAZORPAY

### Webhooks
- **Signature Header**: `X-Razorpay-Signature`. The signature is an HMAC-SHA256 hash of the **raw request body** using the `RAZORPAY_WEBHOOK_SECRET` as the key, encoded as a hexadecimal string. [VERIFIED](https://razorpay.com/docs/webhooks/validate-test)
- **Event ID**: Found in the header `x-razorpay-event-id`. Used for idempotency. [VERIFIED](https://razorpay.com/docs/webhooks/best-practices/)
- **Event Names**:
    - `payment.failed`: Triggered when a payment fails. [VERIFIED](https://razorpay.com/docs/webhooks/payments/)
    - `payment.captured`: Triggered when funds are successfully captured. [VERIFIED](https://razorpay.com/docs/webhooks/payments/)
    - `payment_link.paid`: Triggered when a Payment Link is paid. [VERIFIED](https://razorpay.com/docs/webhooks/payment-links)

### Event Payloads
#### General Fields Mapping
For `payment.failed` and `payment.captured`, these fields are located in `payload.payment.entity`:
- **payment id**: `id`
- **order id**: `order_id`
- **amount**: `amount` (Unit: smallest currency unit, e.g., paise for INR)
- **currency**: `currency`
- **method**: `method`
- **bank**: `bank`
- **status**: `status`
- **email**: `email`
- **contact**: `contact`
- **error_code**: `error_code`
- **error_reason**: `error_description` / `error_reason`
- **error_source**: `error_source`
- **error_step**: `error_step`
- **created_at**: `created_at` (Unix timestamp)
[VERIFIED](https://razorpay.com/docs/webhooks/payment-links)

#### `payment_link.paid` Payload
This event contains three entities: `payment`, `order`, and `payment_link`.
- **link id**: `payload.payment_link.entity.id`
- **payment id**: `payload.payment.entity.id`
- **order id**: `payload.payment.entity.order_id` (also in `payload.order.entity.id`)
[VERIFIED](https://razorpay.com/docs/webhooks/payment-links)

### Payment Statuses
| Status | Meaning | Money Status |
|---|---|---|
| `created` | Initial state; details sent but not processed. | Not deducted |
| `authorized` | Bank authenticated details; funds held by Razorpay. | Deducted from customer $\rightarrow$ Held by Razorpay |
| `captured` | Payment verified and complete; scheduled for settlement. | Held by Razorpay $\rightarrow$ Settled to Merchant |
| `failed` | Payment attempt unsuccessful. | Not deducted (or pending refund) |
[VERIFIED](https://razorpay.com/docs/us/payments/payments)

### API Operations
- **Fetch Payment**: Use `GET /v1/payments/{payment_id}`. [VERIFIED](https://razorpay.com/docs/api/payments/fetch-payments-orders/)
- **Check Order Paid**: Use `GET /v1/orders/{order_id}` and check if `status == "paid"`. [VERIFIED](https://razorpay.com/docs/api/orders/fetch-with-id/?preferred-country=US)
- **Create Payment Link**:
    - **Required**: `amount` (smallest unit), `currency`. [VERIFIED](https://razorpay.com/docs/api/payments/payment-links/create-standard/)
    - **Optional**: `reference_id` (max 40 chars, unique), `notes` (max 15 pairs), `expire_by` (Unix timestamp), `customer` (name, email, contact). [VERIFIED](https://razorpay.com/docs/api/payments/payment-links/create-standard/)
    - **Disable Notifications**: Set `notify: { "sms": false, "email": false }` to turn off Razorpay's own notifications. [VERIFIED](https://razorpay.com/docs/api/payments/payment-links/create-standard/)

### Test Mode Simulation
- **Failed Payment**: 
    - Card: Use Test Card $\rightarrow$ enter OTP with $< 4$ digits.
    - UPI: Use `failure@razorpay`.
    - Netbanking: Click "Failure" button on mock page. [VERIFIED](https://razorpay.com/docs/developer-tools/integrations/standard-checkout/)
- **Captured Payment**: 
    - Enable **Auto-Capture** in Dashboard $\rightarrow$ Account & Settings $\rightarrow$ Payment Capture.
    - Or manually call `POST /v1/payments/{payment_id}/capture`. [VERIFIED](https://razorpay.com/docs/api/payments/capture)

---

## GROQ / LANGCHAIN

### Structured Output
- **Model**: `openai/gpt-oss-120b` and `openai/gpt-oss-20b` support native JSON schema structured output with `strict=True`. [VERIFIED](https://reference.langchain.com/python/langchain-groq/chat_models/ChatGroq/with_structured_output)
- **Implementation**: `with_structured_output(schema, method="json_schema", strict=True)` works with `langchain-groq`. [VERIFIED](https://reference.langchain.com/python/langchain-groq/chat_models/ChatGroq/with_structured_output)
- **Strict Mode Constraints**: All fields must be required, and `additionalProperties` must be `false`. [VERIFIED](https://reference.langchain.com/python/langchain-groq/chat_models/ChatGroq/with_structured_output)
- **Packages**: 
    - `langchain-groq` (v0.3.8+ for `json_schema` support). [VERIFIED](https://www.pypi.org/project/langchain-groq)
    - `groq` (v0.30.0+). [VERIFIED](https://www.pypi.org/project/langchain-groq)
    - `langchain-core` (v1.4.0+). [VERIFIED](https://www.pypi.org/project/langchain-groq)

---

## CELERY / REDIS

### SSL Connection
- **URL**: Use `rediss://` (extra 's') for SSL. [VERIFIED](https://redis.io/docs/latest/operate/rc/security/database-security/tls-ssl/)
- **Configuration**:
    - Set `broker_use_ssl = { 'ssl_cert_reqs': ssl.CERT_REQUIRED, 'ssl_ca_certs': '/path/to/ca.pem' }`.
    - Set `redis_backend_use_ssl = { 'ssl_cert_reqs': ssl.CERT_REQUIRED, 'ssl_ca_certs': '/path/to/ca.pem' }`. [VERIFIED](https://github.com/celery/celery/issues/10312)
    - Alternatively, use query param `?ssl_cert_reqs=none` for unverified SSL. [VERIFIED](https://github.com/celery/celery/blob/master/celery/backends/redis.py)

---

## RESEND

### Email Sending
- **Python SDK**: `pip install resend`. Use `resend.Emails.send(params)` or `await resend.Emails.send_async(params)`. [VERIFIED](https://resend.com/docs/send-with-python)
- **HTTP API**: `POST https://api.resend.com/emails` with `Authorization: Bearer <api_key>`. [VERIFIED](https://resend.com/docs/api-reference/emails)
- **Test Mode Sender**: Using `onboarding@resend.dev` restricts recipients to:
    - Your own account email.
    - `delivered@resend.dev`, `bounced@resend.dev`, `complained@resend.dev`, `suppressed@resend.dev`. [VERIFIED](https://resend.com/docs/knowledge-base/403-error-resend-dev-domain)
- **Idempotency**: Supported via `idempotency_key` parameter. Keys are kept for **24 hours**. [VERIFIED](https://resend.com/docs/dashboard/emails/idempotency-keys)
- **Rate Limits**: 
    - API: 10 requests per second per team. [VERIFIED](https://resend.com/docs/api-reference/rate-limit)
    - Free Plan: 100 emails/day, 3,000 emails/month. [VERIFIED](https://resend.com/docs/knowledge-base/account-quotas-and-limits)

---

## Questions for the owner
- None. All requested facts were found and verified.
