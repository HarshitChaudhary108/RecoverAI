CREATE TABLE IF NOT EXISTS webhook_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS payments (
    payment_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    amount BIGINT NOT NULL,
    currency TEXT NOT NULL,
    method TEXT,
    bank TEXT,
    status TEXT NOT NULL,
    error_code TEXT,
    error_reason TEXT,
    error_source TEXT,
    error_step TEXT,
    customer_email TEXT,
    customer_contact TEXT,
    payment_created_at TIMESTAMPTZ,
    failed_at TIMESTAMPTZ,
    captured_at TIMESTAMPTZ,
    classification_status TEXT NOT NULL CHECK (
        classification_status IN ('pending', 'classified', 'failed')
    ),
    failure_category TEXT,
    classification_confidence DOUBLE PRECISION,
    classification_reason TEXT,
    classification_error TEXT,
    classification_attempts INTEGER NOT NULL DEFAULT 0,
    recovery_group TEXT CHECK (
        recovery_group IN ('treatment', 'holdout', 'not_eligible')
    ),
    recovery_status TEXT NOT NULL CHECK (
        recovery_status IN ('open', 'recovered', 'self_recovered')
    ),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_payments_order_id
    ON payments(order_id);

CREATE INDEX IF NOT EXISTS idx_payments_classification_retry
    ON payments(classification_status, classification_attempts, updated_at);

CREATE TABLE IF NOT EXISTS scheduled_actions (
    action_id TEXT PRIMARY KEY,
    payment_id TEXT NOT NULL REFERENCES payments(payment_id),
    action_type TEXT NOT NULL,
    step INTEGER NOT NULL,
    run_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'claimed', 'completed', 'cancelled', 'failed', 'manual_review')
    ),
    result TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    recheck_count INTEGER NOT NULL DEFAULT 0,
    locked_by TEXT,
    lease_expires_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    UNIQUE (payment_id, action_type, step)
);

CREATE INDEX IF NOT EXISTS idx_scheduled_actions_due
    ON scheduled_actions(status, run_at);

CREATE TABLE IF NOT EXISTS recovery_attempts (
    id TEXT PRIMARY KEY,
    action_id TEXT UNIQUE NOT NULL REFERENCES scheduled_actions(action_id),
    original_payment_id TEXT NOT NULL REFERENCES payments(payment_id),
    link_id TEXT UNIQUE,
    link_url TEXT,
    channel TEXT,
    recovery_group TEXT,
    delay_used INTERVAL,
    sent_at TIMESTAMPTZ,
    status TEXT,
    recovered_payment_id TEXT REFERENCES payments(payment_id),
    recovered_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_recovery_attempts_original_payment_sent
    ON recovery_attempts(original_payment_id, sent_at);

CREATE TABLE IF NOT EXISTS health_snapshots (
    ts TIMESTAMPTZ NOT NULL,
    scope TEXT NOT NULL,
    scope_value TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    captured INTEGER NOT NULL,
    failed INTEGER NOT NULL,
    success_rate DOUBLE PRECISION,
    PRIMARY KEY (ts, scope, scope_value)
);

CREATE TABLE IF NOT EXISTS alert_state (
    scope TEXT NOT NULL,
    scope_value TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('ok', 'alerting')),
    healthy_streak INTEGER NOT NULL DEFAULT 0,
    last_success_rate DOUBLE PRECISION,
    last_baseline DOUBLE PRECISION,
    last_attempts INTEGER,
    state_changed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (scope, scope_value)
);
