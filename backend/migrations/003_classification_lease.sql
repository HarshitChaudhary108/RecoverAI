BEGIN;

-- Prevent duplicate/stale classification tasks from performing concurrent LLM work.
-- A short database transaction claims the payment; the LLM call happens outside
-- the transaction; the result is committed only by the worker holding the lease.

ALTER TABLE payments
    DROP CONSTRAINT IF EXISTS payments_classification_status_check;

ALTER TABLE payments
    ADD CONSTRAINT payments_classification_status_check
    CHECK (
        classification_status IN ('pending', 'processing', 'classified', 'failed')
    );

ALTER TABLE payments
    ADD COLUMN IF NOT EXISTS classification_lease_id TEXT;

ALTER TABLE payments
    ADD COLUMN IF NOT EXISTS classification_lease_expires_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_payments_classification_lease
    ON payments(
        classification_status,
        classification_lease_expires_at,
        classification_attempts,
        updated_at
    );

COMMIT;
