-- Webhook events deduplication and persistence
CREATE TABLE IF NOT EXISTS webhook_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    received_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- Payments state and classification
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

    -- Classification metadata
    classification_status TEXT NOT NULL CHECK (classification_status IN ('pending', 'classified', 'failed')),
    failure_category TEXT,
    classification_confidence FLOAT,
    classification_reason TEXT,
    classification_error TEXT,
    classification_attempts INTEGER DEFAULT 0,

    -- Recovery state
    recovery_group TEXT CHECK (recovery_group IN ('treatment', 'holdout', 'not_eligible')),
    recovery_status TEXT NOT NULL CHECK (recovery_status IN ('open', 'recovered', 'self_recovered')),

    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_payments_order_id ON payments(order_id);

-- Scheduled recovery actions
CREATE TABLE IF NOT EXISTS scheduled_actions (
    action_id TEXT PRIMARY KEY,
    payment_id TEXT NOT NULL REFERENCES payments(payment_id),
    action_type TEXT NOT NULL,
    step INTEGER NOT NULL,
    run_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'claimed', 'completed', 'cancelled', 'failed', 'manual_review')),
    result TEXT,
    attempts INTEGER DEFAULT 0,
    locked_by TEXT,
    lease_expires_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,

    UNIQUE (payment_id, action_type, step)
);

CREATE INDEX IF NOT EXISTS idx_scheduled_actions_due ON scheduled_actions(status, run_at);

-- Recovery attempt history
CREATE TABLE IF NOT EXISTS recovery_attempts (
    id TEXT PRIMARY KEY,
    action_id TEXT UNIQUE NOT NULL REFERENCES scheduled_actions(action_id),
    original_payment_id TEXT NOT NULL REFERENCES payments(payment_id),
    link_id TEXT,
    link_url TEXT,
    channel TEXT,
    recovery_group TEXT,
    delay_used INTEGER,
    sent_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    status TEXT,
    recovered_payment_id TEXT REFERENCES payments(payment_id),
    recovered_at TIMESTAMPTZ
);

-- Health snapshots for monitoring
CREATE TABLE IF NOT EXISTS health_snapshots (
    ts TIMESTAMPTZ NOT NULL,
    scope TEXT NOT NULL,
    scope_value TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    captured INTEGER NOT NULL,
    failed INTEGER NOT NULL,
    success_rate FLOAT,
    PRIMARY KEY (ts, scope, scope_value)
);

-- Alert state for dashboard hysteresis
CREATE TABLE IF NOT EXISTS alert_state (
    scope TEXT NOT NULL,
    scope_value TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('ok', 'alerting')),
    healthy_streak INTEGER DEFAULT 0,
    last_success_rate FLOAT,
    last_baseline FLOAT,
    last_attempts INTEGER,
    state_changed_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (scope, scope_value)
);
