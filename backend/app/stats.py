from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Query

from backend.app.db import get_db_cursor
from backend.app.schemas import (
    AlertStat,
    EntityStat,
    FailureReasonStat,
    FunnelStats,
    SummaryStats,
    TimeSeriesPoint,
)

router = APIRouter()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StatsService:
    """Read-only PostgreSQL-backed dashboard statistics."""

    def _get_start_time(self, hours: int) -> datetime:
        if hours <= 0:
            raise ValueError("hours must be positive")
        return _utc_now() - timedelta(hours=hours)

    def get_summary(self, hours: int) -> SummaryStats:
        start_time = self._get_start_time(hours)
        with get_db_cursor() as cur:
            cur.execute(
                """
                WITH windowed AS (
                    SELECT *
                    FROM payments
                    WHERE COALESCE(payment_created_at, failed_at) >= %s
                )
                SELECT
                    COUNT(*) FILTER (WHERE status = 'captured') AS captured,
                    COUNT(*) FILTER (
                        WHERE status = 'failed'
                          AND COALESCE(failure_category, '') <> 'user_cancelled'
                    ) AS failed_effective,
                    COUNT(*) FILTER (WHERE status = 'failed') AS failed_count,
                    COUNT(*) FILTER (WHERE recovery_status = 'recovered') AS recovered_count,
                    COALESCE(SUM(amount) FILTER (WHERE recovery_status = 'recovered'), 0) AS revenue_recovered,
                    COALESCE(
                        SUM(amount) FILTER (
                            WHERE status = 'failed'
                              AND recovery_status = 'open'
                              AND failed_at IS NOT NULL
                              AND failed_at >= %s
                        ),
                        0
                    ) AS revenue_at_risk
                FROM windowed
                """,
                (start_time, _utc_now() - timedelta(days=7)),
            )
            row = cur.fetchone()

        if not row:
            return SummaryStats(
                success_rate=0.0,
                failed_count=0,
                recovered_count=0,
                revenue_recovered=0,
                revenue_at_risk=0,
            )

        captured, failed_effective, failed_count, recovered_count, revenue_recovered, revenue_at_risk = row
        denominator = int(captured or 0) + int(failed_effective or 0)
        success_rate = (float(captured or 0) / denominator * 100.0) if denominator else 0.0

        return SummaryStats(
            success_rate=success_rate,
            failed_count=int(failed_count or 0),
            recovered_count=int(recovered_count or 0),
            revenue_recovered=int(revenue_recovered or 0),
            revenue_at_risk=int(revenue_at_risk or 0),
        )

    def get_timeseries(self, hours: int):
        start_time = self._get_start_time(hours)
        with get_db_cursor() as cur:
            cur.execute(
                """
                SELECT ts, success_rate, attempts, captured, failed
                FROM health_snapshots
                WHERE scope = 'global' AND scope_value = 'all'
                  AND ts >= %s
                ORDER BY ts ASC
                """,
                (start_time,),
            )
            rows = cur.fetchall()
        return [
            TimeSeriesPoint(
                ts=row[0],
                success_rate=float(row[1] or 0.0) * 100.0,
                attempts=int(row[2] or 0),
                captured=int(row[3] or 0),
                failed=int(row[4] or 0),
            )
            for row in rows
        ]

    def get_failure_reasons(self, hours: int):
        start_time = self._get_start_time(hours)
        with get_db_cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(failure_category, 'unclassified') AS reason,
                       COUNT(*) AS count
                FROM payments
                WHERE status = 'failed'
                  AND COALESCE(payment_created_at, failed_at) >= %s
                GROUP BY COALESCE(failure_category, 'unclassified')
                ORDER BY count DESC
                """,
                (start_time,),
            )
            rows = cur.fetchall()
        return [FailureReasonStat(reason=row[0], count=int(row[1])) for row in rows]

    def get_entity_stats(self, hours: int, dimension: str):
        if dimension not in {"bank", "method"}:
            raise ValueError("dimension must be bank or method")
        start_time = self._get_start_time(hours)
        column = "bank" if dimension == "bank" else "method"
        with get_db_cursor() as cur:
            cur.execute(
                f"""
                SELECT {column},
                       COUNT(*) FILTER (WHERE status = 'captured') AS captured,
                       COUNT(*) FILTER (
                           WHERE status = 'failed'
                             AND COALESCE(failure_category, '') <> 'user_cancelled'
                       ) AS failed
                FROM payments
                WHERE COALESCE(payment_created_at, failed_at) >= %s
                  AND {column} IS NOT NULL
                  AND {column} <> ''
                GROUP BY {column}
                ORDER BY {column}
                """,
                (start_time,),
            )
            rows = cur.fetchall()

        results = []
        for value, captured, failed in rows:
            captured = int(captured or 0)
            failed = int(failed or 0)
            attempts = captured + failed
            rate = (captured / attempts * 100.0) if attempts else 0.0
            results.append(
                EntityStat(
                    entity=value,
                    attempts=attempts,
                    captured=captured,
                    failed=failed,
                    success_rate=rate,
                )
            )
        return results

    def get_funnel(self, hours: int) -> FunnelStats:
        start_time = self._get_start_time(hours)
        with get_db_cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE status = 'failed') AS failed,
                    COUNT(*) FILTER (
                        WHERE status = 'failed'
                          AND recovery_group = 'treatment'
                    ) AS treatment,
                    COUNT(*) FILTER (
                        WHERE status = 'failed'
                          AND recovery_group = 'holdout'
                    ) AS holdout,
                    COUNT(DISTINCT ra.id) FILTER (
                        WHERE p.status = 'failed'
                          AND ra.status IN ('sent', 'recovered')
                    ) AS messages_sent,
                    COUNT(DISTINCT ra.id) FILTER (
                        WHERE p.status = 'failed'
                          AND ra.status = 'recovered'
                    ) AS recovered
                FROM payments AS p
                LEFT JOIN recovery_attempts AS ra
                  ON ra.original_payment_id = p.payment_id
                WHERE COALESCE(p.payment_created_at, p.failed_at) >= %s
                """,
                (start_time,),
            )
            failed, treatment, holdout, messages_sent, recovered = cur.fetchone()

            cur.execute(
                """
                SELECT COUNT(DISTINCT ra.id)
                FROM payments AS p
                JOIN recovery_attempts AS ra
                  ON ra.original_payment_id = p.payment_id
                WHERE COALESCE(p.payment_created_at, p.failed_at) >= %s
                  AND p.recovery_group = 'treatment'
                  AND ra.status = 'recovered'
                """,
                (start_time,),
            )
            treatment_recovered = int(cur.fetchone()[0] or 0)

            cur.execute(
                """
                SELECT COUNT(*)
                FROM payments
                WHERE COALESCE(payment_created_at, failed_at) >= %s
                  AND recovery_group = 'holdout'
                  AND recovery_status IN ('recovered', 'self_recovered')
                """,
                (start_time,),
            )
            holdout_recovered = int(cur.fetchone()[0] or 0)

        return FunnelStats(
            failed=int(failed or 0),
            treatment=int(treatment or 0),
            holdout=int(holdout or 0),
            messages_sent=int(messages_sent or 0),
            recovered=int(recovered or 0),
            treatment_recovered=treatment_recovered,
            holdout_recovered=holdout_recovered,
        )

    def get_alerts(self):
        with get_db_cursor() as cur:
            cur.execute(
                """
                SELECT scope, scope_value, state,
                       last_success_rate, last_baseline,
                       last_attempts, healthy_streak,
                       state_changed_at, updated_at
                FROM alert_state
                ORDER BY scope, scope_value
                """
            )
            rows = cur.fetchall()
        return [
            AlertStat(
                scope=row[0],
                scope_value=row[1],
                state=row[2],
                current_success_rate=float(row[3] or 0.0) * 100.0,
                baseline=(float(row[4]) * 100.0 if row[4] is not None else None),
                attempts=int(row[5] or 0),
                healthy_streak=int(row[6] or 0),
                state_changed_at=row[7],
                updated_at=row[8],
            )
            for row in rows
        ]


service = StatsService()


@router.get("/stats/summary", response_model=SummaryStats)
def summary(hours: int = Query(24, ge=1, le=24 * 30)):
    return service.get_summary(hours)


@router.get("/stats/timeseries", response_model=list[TimeSeriesPoint])
def timeseries(hours: int = Query(24, ge=1, le=24 * 30)):
    return service.get_timeseries(hours)


@router.get("/stats/failure-reasons", response_model=list[FailureReasonStat])
def failure_reasons(hours: int = Query(24, ge=1, le=24 * 30)):
    return service.get_failure_reasons(hours)


@router.get("/stats/by-bank", response_model=list[EntityStat])
def by_bank(hours: int = Query(1, ge=1, le=24 * 30)):
    return service.get_entity_stats(hours, "bank")


@router.get("/stats/by-method", response_model=list[EntityStat])
def by_method(hours: int = Query(1, ge=1, le=24 * 30)):
    return service.get_entity_stats(hours, "method")


@router.get("/recovery/funnel", response_model=FunnelStats)
def recovery_funnel(hours: int = Query(24, ge=1, le=24 * 30)):
    return service.get_funnel(hours)


@router.get("/alerts", response_model=list[AlertStat])
def alerts():
    return service.get_alerts()
