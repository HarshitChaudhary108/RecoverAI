import logging
from datetime import datetime, timedelta
from backend.app.db import get_db_cursor
from backend.app.config import settings

logger = logging.getLogger(__name__)

def run_snapshot_health():
    """
    Calculates payment health metrics for global, bank, and method scopes
    over the configured health window and saves them as a snapshot.
    """
    logger.info("Generating health snapshots...")

    # Timestamp truncated to the minute
    now = datetime.utcnow()
    ts = now.replace(second=0, microsecond=0)

    # Window for calculations
    window_start = ts - timedelta(minutes=settings.HEALTH_WINDOW_MINUTES)

    with get_db_cursor() as cur:
        # 1. Global Scope
        cur.execute("""
            SELECT
                COUNT(*) as attempts,
                COUNT(*) FILTER (WHERE status = 'captured') as captured,
                COUNT(*) FILTER (WHERE status = 'failed' AND failure_category != 'user_cancelled') as failed
            FROM payments
            WHERE payment_created_at >= %s
        """, (window_start,))

        global_metrics = cur.fetchone()
        save_snapshot(cur, ts, 'global', 'all', global_metrics)

        # 2. Bank Scope
        cur.execute("""
            SELECT
                bank,
                COUNT(*) as attempts,
                COUNT(*) FILTER (WHERE status = 'captured') as captured,
                COUNT(*) FILTER (WHERE status = 'failed' AND failure_category != 'user_cancelled') as failed
            FROM payments
            WHERE payment_created_at >= %s
              AND bank IS NOT NULL AND bank != ''
            GROUP BY bank
        """, (window_start,))

        for row in cur.fetchall():
            bank, attempts, captured, failed = row
            save_snapshot(cur, ts, 'bank', bank, (attempts, captured, failed))

        # 3. Method Scope
        cur.execute("""
            SELECT
                method,
                COUNT(*) as attempts,
                COUNT(*) FILTER (WHERE status = 'captured') as captured,
                COUNT(*) FILTER (WHERE status = 'failed' AND failure_category != 'user_cancelled') as failed
            FROM payments
            WHERE payment_created_at >= %s
              AND method IS NOT NULL AND method != ''
            GROUP BY method
        """, (window_start,))

        for row in cur.fetchall():
            method, attempts, captured, failed = row
            save_snapshot(cur, ts, 'method', method, (attempts, captured, failed))

def save_snapshot(cur, ts, scope, scope_value, metrics):
    """Helper to calculate success rate and persist snapshot with ON CONFLICT."""
    attempts, captured, failed = metrics

    # Handle case where no payments match (attempts = 0)
    if attempts == 0:
        success_rate = None
    else:
        # Spec: attempts = captured + failed
        # But count(*) in the query is total. Let's be precise based on requirements:
        # "captured = payments with status captured. failed = payments with status failed,
        #  but NOT category user_cancelled. attempts = captured + failed."
        # This means we should use the sum of captured and failed as the denominator.

        # Recalculate attempts for the success rate based on the spec
        effective_attempts = captured + failed
        if effective_attempts == 0:
            success_rate = None
        else:
            success_rate = captured / effective_attempts

    # We use effective_attempts for the 'attempts' column as per spec
    effective_attempts = captured + failed

    cur.execute("""
        INSERT INTO health_snapshots (ts, scope, scope_value, attempts, captured, failed, success_rate)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (ts, scope, scope_value) DO UPDATE SET
            attempts = EXCLUDED.attempts,
            captured = EXCLUDED.captured,
            failed = EXCLUDED.failed,
            success_rate = EXCLUDED.success_rate
    """, (ts, scope, scope_value, effective_attempts, captured, failed, success_rate))
