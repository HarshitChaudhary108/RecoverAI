import logging
from datetime import datetime, timedelta, timezone

from backend.app.config import settings
from backend.app.db import get_db_cursor

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _success_rate(captured: int, failed: int) -> float:
    attempts = captured + failed
    return (captured / attempts) if attempts else 0.0


def save_snapshot(cur, ts: datetime, scope: str, scope_value: str, captured: int, failed: int) -> None:
    captured = int(captured or 0)
    failed = int(failed or 0)
    attempts = captured + failed
    success_rate = _success_rate(captured, failed)

    cur.execute(
        """
        INSERT INTO health_snapshots (
            ts, scope, scope_value, attempts, captured, failed, success_rate
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (ts, scope, scope_value) DO UPDATE SET
            attempts = EXCLUDED.attempts,
            captured = EXCLUDED.captured,
            failed = EXCLUDED.failed,
            success_rate = EXCLUDED.success_rate
        """,
        (ts, scope, scope_value, attempts, captured, failed, success_rate),
    )


def run_snapshot_health() -> None:
    """Generate one-minute global, bank, and method health snapshots."""
    now = _utc_now()
    ts = now.replace(second=0, microsecond=0)
    window_start = ts - timedelta(minutes=settings.HEALTH_WINDOW_MINUTES)

    with get_db_cursor() as cur:
        cur.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE status = 'captured') AS captured,
                COUNT(*) FILTER (
                    WHERE status = 'failed'
                      AND COALESCE(failure_category, '') <> 'user_cancelled'
                ) AS failed
            FROM payments
            WHERE COALESCE(payment_created_at, failed_at) >= %s
              AND status IN ('captured', 'failed')
            """,
            (window_start,),
        )
        captured, failed = cur.fetchone()
        save_snapshot(cur, ts, "global", "all", captured, failed)

        cur.execute(
            """
            SELECT
                bank,
                COUNT(*) FILTER (WHERE status = 'captured') AS captured,
                COUNT(*) FILTER (
                    WHERE status = 'failed'
                      AND COALESCE(failure_category, '') <> 'user_cancelled'
                ) AS failed
            FROM payments
            WHERE COALESCE(payment_created_at, failed_at) >= %s
              AND status IN ('captured', 'failed')
              AND bank IS NOT NULL
              AND bank <> ''
            GROUP BY bank
            """,
            (window_start,),
        )
        for bank, captured, failed in cur.fetchall():
            save_snapshot(cur, ts, "bank", bank, captured, failed)

        cur.execute(
            """
            SELECT
                method,
                COUNT(*) FILTER (WHERE status = 'captured') AS captured,
                COUNT(*) FILTER (
                    WHERE status = 'failed'
                      AND COALESCE(failure_category, '') <> 'user_cancelled'
                ) AS failed
            FROM payments
            WHERE COALESCE(payment_created_at, failed_at) >= %s
              AND status IN ('captured', 'failed')
              AND method IS NOT NULL
              AND method <> ''
            GROUP BY method
            """,
            (window_start,),
        )
        for method, captured, failed in cur.fetchall():
            save_snapshot(cur, ts, "method", method, captured, failed)

    logger.info("Health snapshot generated at %s", ts.isoformat())
