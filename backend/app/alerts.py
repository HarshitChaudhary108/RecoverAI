import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from backend.app.config import settings
from backend.app.db import get_db_cursor

logger = logging.getLogger(__name__)


def next_state(previous_state: str, healthy_streak: int, condition_true: bool):
    """Apply the configured two-check alert-state hysteresis."""
    if previous_state == "ok":
        return ("alerting", 0) if condition_true else ("ok", 0)

    if previous_state == "alerting":
        if condition_true:
            return "alerting", 0
        healthy_streak += 1
        if healthy_streak >= settings.ALERT_HEALTHY_CHECKS_TO_CLEAR:
            return "ok", 0
        return "alerting", healthy_streak

    return "ok", 0


def run_evaluate_alerts() -> None:
    """Evaluate the latest health snapshot and persist dashboard-only alert state."""
    with get_db_cursor() as cur:
        cur.execute("SELECT MAX(ts) FROM health_snapshots")
        row = cur.fetchone()
        if not row or not row[0]:
            logger.warning("No health snapshots available for alert evaluation")
            return

        latest_ts = row[0]
        latest_local_hour = latest_ts.astimezone(
            ZoneInfo(settings.QUIET_HOURS_TIMEZONE)
        ).hour
        baseline_start = latest_ts - timedelta(days=settings.ALERT_BASELINE_DAYS)

        cur.execute(
            """
            SELECT scope, scope_value, success_rate, attempts
            FROM health_snapshots
            WHERE ts = %s
            """,
            (latest_ts,),
        )
        snapshots = cur.fetchall()

        for scope, scope_value, success_rate, attempts in snapshots:
            current_rate = float(success_rate or 0.0)
            attempts = int(attempts or 0)

            cur.execute(
                """
                SELECT AVG(success_rate)
                FROM health_snapshots
                WHERE scope = %s
                  AND scope_value = %s
                  AND ts >= %s
                  AND ts < %s
                  AND EXTRACT(HOUR FROM ts AT TIME ZONE 'Asia/Kolkata') = %s
                """,
                (scope, scope_value, baseline_start, latest_ts, latest_local_hour),
            )
            baseline_row = cur.fetchone()
            baseline = baseline_row[0] if baseline_row else None
            baseline = float(baseline) if baseline is not None else None

            floor_breached = current_rate < settings.ALERT_SUCCESS_FLOOR_PERCENT / 100.0
            baseline_breached = (
                baseline is not None
                and current_rate < baseline - settings.ALERT_DROP_THRESHOLD_PERCENT / 100.0
            )
            condition_true = attempts >= settings.ALERT_MIN_ATTEMPTS and (
                floor_breached or baseline_breached
            )

            cur.execute(
                """
                SELECT state, healthy_streak
                FROM alert_state
                WHERE scope = %s AND scope_value = %s
                FOR UPDATE
                """,
                (scope, scope_value),
            )
            state_row = cur.fetchone()
            if state_row:
                previous_state, healthy_streak = state_row
            else:
                previous_state, healthy_streak = "ok", 0

            new_state, new_streak = next_state(
                previous_state,
                int(healthy_streak or 0),
                condition_true,
            )

            cur.execute(
                """
                INSERT INTO alert_state (
                    scope, scope_value, state, healthy_streak,
                    last_success_rate, last_baseline, last_attempts,
                    state_changed_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (scope, scope_value) DO UPDATE SET
                    state = EXCLUDED.state,
                    healthy_streak = EXCLUDED.healthy_streak,
                    last_success_rate = EXCLUDED.last_success_rate,
                    last_baseline = EXCLUDED.last_baseline,
                    last_attempts = EXCLUDED.last_attempts,
                    state_changed_at = CASE
                        WHEN alert_state.state <> EXCLUDED.state
                        THEN CURRENT_TIMESTAMP
                        ELSE alert_state.state_changed_at
                    END,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    scope,
                    scope_value,
                    new_state,
                    new_streak,
                    current_rate,
                    baseline,
                    attempts,
                ),
            )

    logger.info("Health alerts evaluated for snapshot %s", latest_ts.isoformat())
