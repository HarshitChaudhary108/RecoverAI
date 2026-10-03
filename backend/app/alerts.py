import logging
from datetime import datetime
from backend.app.db import get_db_cursor
from backend.app.config import settings

logger = logging.getLogger(__name__)

def next_state(previous_state, healthy_streak, condition_true):
    """
    Pure function to determine the next alert state based on hysteresis rules.

    Args:
        previous_state (str): 'ok' or 'alerting'
        healthy_streak (int): number of consecutive healthy checks
        condition_true (bool): whether the alert condition is currently met

    Returns:
        tuple: (new_state, new_streak)
    """
    if previous_state == 'ok':
        if condition_true:
            return 'alerting', 0
        return 'ok', 0

    if previous_state == 'alerting':
        if condition_true:
            return 'alerting', 0
        else:
            new_streak = healthy_streak + 1
            if new_streak >= settings.ALERT_HEALTHY_CHECKS_TO_CLEAR:
                return 'ok', 0
            return 'alerting', new_streak

    return 'ok', 0

def run_evaluate_alerts():
    """
    Evaluates the latest health snapshots and updates alert states.
    """
    logger.info("Evaluating health alerts...")

    with get_db_cursor() as cur:
        # 1. Get the latest snapshot timestamp
        cur.execute("SELECT MAX(ts) FROM health_snapshots")
        latest_ts_row = cur.fetchone()
        if not latest_ts_row or not latest_ts_row[0]:
            logger.warning("No health snapshots found to evaluate.")
            return

        latest_ts = latest_ts_row[0]
        current_hour = latest_ts.hour

        # 2. Fetch all snapshots for the latest timestamp
        cur.execute("""
            SELECT scope, scope_value, success_rate, attempts
            FROM health_snapshots
            WHERE ts = %s
        """, (latest_ts,))

        snapshots = cur.fetchall()

        for scope, scope_value, success_rate, attempts in snapshots:
            # Baseline: avg success rate in the same hour of day over last 7 days
            cur.execute("""
                SELECT AVG(success_rate)
                FROM health_snapshots
                WHERE scope = %s
                  AND scope_value = %s
                  AND EXTRACT(HOUR FROM ts) = %s
                  AND ts >= %s - interval '7 days'
            """, (scope, scope_value, current_hour, latest_ts))

            baseline_row = cur.fetchone()
            baseline = baseline_row[0] if baseline_row else None

            # Condition check
            condition_true = False
            if attempts >= settings.ALERT_MIN_ATTEMPTS:
                floor = settings.ALERT_SUCCESS_FLOOR_PERCENT / 100.0
                drop_threshold = settings.ALERT_DROP_THRESHOLD_PERCENT / 100.0

                # Trigger if below floor OR below baseline minus drop
                if success_rate is not None:
                    if success_rate < floor:
                        condition_true = True
                    elif baseline is not None and success_rate < (baseline - drop_threshold):
                        condition_true = True

            # Fetch current state
            cur.execute("""
                SELECT state, healthy_streak
                FROM alert_state
                WHERE scope = %s AND scope_value = %s
            """, (scope, scope_value))
            state_row = cur.fetchone()

            prev_state = state_row[0] if state_row else 'ok'
            prev_streak = state_row[1] if state_row else 0

            # Transition
            new_state, new_streak = next_state(prev_state, prev_streak, condition_true)

            # Persist new state
            cur.execute("""
                INSERT INTO alert_state (scope, scope_value, state, healthy_streak, last_success_rate, last_baseline, last_attempts, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (scope, scope_value) DO UPDATE SET
                    state = EXCLUDED.state,
                    healthy_streak = EXCLUDED.healthy_streak,
                    last_success_rate = EXCLUDED.last_success_rate,
                    last_baseline = EXCLUDED.last_baseline,
                    last_attempts = EXCLUDED.last_attempts,
                    state_changed_at = CASE WHEN alert_state.state != EXCLUDED.state THEN EXCLUDED.updated_at ELSE alert_state.state_changed_at END,
                    updated_at = EXCLUDED.updated_at
            """, (scope, scope_value, new_state, new_streak, success_rate, baseline, attempts, datetime.utcnow()))

            if new_state != prev_state:
                logger.info(f"ALERT STATE CHANGE: {scope}:{scope_value} {prev_state} -> {new_state} "
                            f"(rate: {success_rate}, baseline: {baseline}, attempts: {attempts})")
