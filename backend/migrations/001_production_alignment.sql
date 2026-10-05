BEGIN;

-- Existing RecoverAI databases may already have the tables but lack the
-- persistent timeout recheck counter or idempotency indexes required by the
-- production worker.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'scheduled_actions'
    ) THEN
        RAISE EXCEPTION 'scheduled_actions table does not exist; initialize schema first';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'recovery_attempts'
    ) THEN
        RAISE EXCEPTION 'recovery_attempts table does not exist; initialize schema first';
    END IF;
END $$;

-- Known legacy application names -> canonical names.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'payments' AND column_name = 'category'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'payments' AND column_name = 'failure_category'
    ) THEN
        ALTER TABLE payments RENAME COLUMN category TO failure_category;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'payments' AND column_name = 'confidence'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'payments' AND column_name = 'classification_confidence'
    ) THEN
        ALTER TABLE payments RENAME COLUMN confidence TO classification_confidence;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'payments' AND column_name = 'reason'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'payments' AND column_name = 'classification_reason'
    ) THEN
        ALTER TABLE payments RENAME COLUMN reason TO classification_reason;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'payments' AND column_name = 'attempts'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'payments' AND column_name = 'classification_attempts'
    ) THEN
        ALTER TABLE payments RENAME COLUMN attempts TO classification_attempts;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'recovery_attempts' AND column_name = 'payment_id'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'recovery_attempts' AND column_name = 'original_payment_id'
    ) THEN
        ALTER TABLE recovery_attempts RENAME COLUMN payment_id TO original_payment_id;
    END IF;
END $$;

ALTER TABLE scheduled_actions
    ADD COLUMN IF NOT EXISTS recheck_count INTEGER;

UPDATE scheduled_actions
SET recheck_count = 0
WHERE recheck_count IS NULL;

ALTER TABLE scheduled_actions
    ALTER COLUMN recheck_count SET DEFAULT 0;

ALTER TABLE scheduled_actions
    ALTER COLUMN recheck_count SET NOT NULL;

-- A recovery attempt is uniquely tied to one scheduled action. Abort instead
-- of silently discarding historical duplicates.
DO $$
BEGIN
    IF EXISTS (
        SELECT action_id
        FROM recovery_attempts
        WHERE action_id IS NOT NULL
        GROUP BY action_id
        HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION
            'Duplicate recovery_attempts.action_id rows exist; reconcile them before applying idempotency index';
    END IF;

    IF EXISTS (
        SELECT link_id
        FROM recovery_attempts
        WHERE link_id IS NOT NULL
        GROUP BY link_id
        HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION
            'Duplicate recovery_attempts.link_id rows exist; reconcile them before applying idempotency index';
    END IF;

    IF EXISTS (
        SELECT payment_id, action_type, step
        FROM scheduled_actions
        GROUP BY payment_id, action_type, step
        HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION
            'Duplicate scheduled_actions business keys exist; reconcile them before applying idempotency index';
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_recovery_attempts_action_id
    ON recovery_attempts(action_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_recovery_attempts_link_id
    ON recovery_attempts(link_id)
    WHERE link_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_scheduled_actions_business_key
    ON scheduled_actions(payment_id, action_type, step);

-- An attempt is not "sent" merely because a Payment Link was created. Remove
-- any pre-existing default that would falsely populate sent_at on link creation.
ALTER TABLE recovery_attempts
    ALTER COLUMN sent_at DROP DEFAULT;

COMMIT;
