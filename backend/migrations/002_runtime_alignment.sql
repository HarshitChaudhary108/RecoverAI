BEGIN;

-- RecoverAI runtime alignment for an existing production database.
-- This migration is intentionally small and non-destructive.
-- It aligns the persistent timeout counter and recovery delay datatype
-- with the production application contract.

-- ------------------------------------------------------------
-- 1. Persistent timeout recheck state
-- ------------------------------------------------------------

ALTER TABLE scheduled_actions
    ADD COLUMN IF NOT EXISTS recheck_count INTEGER;

UPDATE scheduled_actions
SET recheck_count = 0
WHERE recheck_count IS NULL;

ALTER TABLE scheduled_actions
    ALTER COLUMN recheck_count SET DEFAULT 0;

ALTER TABLE scheduled_actions
    ALTER COLUMN recheck_count SET NOT NULL;


-- ------------------------------------------------------------
-- 2. Recovery-attempt delay must be an INTERVAL.
--
-- Existing databases may contain INTEGER minutes from an older
-- application version. Convert those values explicitly to minutes.
-- ------------------------------------------------------------

DO $$
DECLARE
    delay_type TEXT;
BEGIN
    SELECT data_type
    INTO delay_type
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'recovery_attempts'
      AND column_name = 'delay_used';

    IF delay_type IS NULL THEN
        ALTER TABLE recovery_attempts
            ADD COLUMN delay_used INTERVAL;
    ELSIF delay_type = 'integer' THEN
        ALTER TABLE recovery_attempts
            ALTER COLUMN delay_used TYPE INTERVAL
            USING make_interval(mins => delay_used);
    ELSIF delay_type <> 'interval' THEN
        RAISE EXCEPTION
            'Unsupported recovery_attempts.delay_used type: %', delay_type;
    END IF;
END $$;


-- ------------------------------------------------------------
-- 3. Recovery-attempt idempotency
-- ------------------------------------------------------------

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
            'Duplicate recovery_attempts.action_id rows exist; reconcile before migration';
    END IF;

    IF EXISTS (
        SELECT link_id
        FROM recovery_attempts
        WHERE link_id IS NOT NULL
        GROUP BY link_id
        HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION
            'Duplicate recovery_attempts.link_id rows exist; reconcile before migration';
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_recovery_attempts_action_id
    ON recovery_attempts(action_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_recovery_attempts_link_id
    ON recovery_attempts(link_id)
    WHERE link_id IS NOT NULL;


-- ------------------------------------------------------------
-- 4. Scheduled-action idempotency
-- ------------------------------------------------------------

DO $$
BEGIN
    IF EXISTS (
        SELECT payment_id, action_type, step
        FROM scheduled_actions
        GROUP BY payment_id, action_type, step
        HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION
            'Duplicate scheduled_actions business keys exist; reconcile before migration';
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_scheduled_actions_business_key
    ON scheduled_actions(payment_id, action_type, step);


-- ------------------------------------------------------------
-- 5. A link_created recovery attempt must not look sent.
-- ------------------------------------------------------------

ALTER TABLE recovery_attempts
    ALTER COLUMN sent_at DROP DEFAULT;

COMMIT;