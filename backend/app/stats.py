from datetime import datetime, timedelta, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query

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


class StatsService:
    """Read-only PostgreSQL-backed dashboard statistics."""

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)

    def _get_start_time(self, hours: int) -> datetime:
        if hours <= 0:
            raise ValueError("hours must be positive")
        return self._utc_now() - timedelta(hours=hours)

    def get_summary(self, hours: int) -> SummaryStats:
        """Return overall payment health and recovery KPIs.

        Success rate is returned as a ratio in [0, 1] because the frontend's
        formatPercent() converts that ratio to a display percentage.
        """
        now = self._utc_now()
        start_time = now - timedelta(hours=hours)
        risk_start = max(start_time, now - timedelta(days=7))

        with get_db_cursor() as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE status = 'captured') AS captured,
                    COUNT(*) FILTER (
                        WHERE status = 'failed'
                          AND COALESCE(failure_category, '') <> 'user_cancelled'
                    ) AS failed_effective,
                    COUNT(*) FILTER (WHERE status = 'failed') AS failed_count,
                    COUNT(*) FILTER (
                        WHERE recovery_status = 'recovered'
                    ) AS recovered_count,
                    COALESCE(
                        SUM(amount) FILTER (
                            WHERE recovery_status = 'recovered'
                        ),
                        0
                    ) AS revenue_recovered,
                    COALESCE(
                        SUM(amount) FILTER (
                            WHERE status = 'failed'
                              AND COALESCE(failure_category, '') <> 'user_cancelled'
                              AND recovery_status = 'open'
                              AND failed_at IS NOT NULL
                              AND failed_at >= %s
                        ),
                        0
                    ) AS revenue_at_risk
                FROM payments
                WHERE COALESCE(payment_created_at, failed_at) >= %s
                """,
                (risk_start, start_time),
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

        (
            captured,
            failed_effective,
            failed_count,
            recovered_count,
            revenue_recovered,
            revenue_at_risk,
        ) = row

        denominator = int(captured or 0) + int(failed_effective or 0)
        success_rate = (float(captured or 0) / denominator) if denominator else 0.0

        return SummaryStats(
            success_rate=success_rate,
            failed_count=int(failed_count or 0),
            recovered_count=int(recovered_count or 0),
            revenue_recovered=int(revenue_recovered or 0),
            revenue_at_risk=int(revenue_at_risk or 0),
        )

    def get_timeseries(self, hours: int) -> List[TimeSeriesPoint]:
        """Return global health snapshots as frontend-compatible points."""
        start_time = self._get_start_time(hours)

        with get_db_cursor() as cur:
            cur.execute(
                """
                SELECT ts, success_rate
                FROM health_snapshots
                WHERE scope = 'global'
                  AND scope_value = 'all'
                  AND ts >= %s
                ORDER BY ts ASC
                """,
                (start_time,),
            )
            rows = cur.fetchall()

        return [
            TimeSeriesPoint(
                timestamp=row[0],
                success_rate=(float(row[1]) if row[1] is not None else None),
            )
            for row in rows
        ]

    def get_failure_reasons(self, hours: int) -> List[FailureReasonStat]:
        start_time = self._get_start_time(hours)

        with get_db_cursor() as cur:
            cur.execute(
                """
                SELECT
                    COALESCE(failure_category, 'unclassified') AS category,
                    COUNT(*) AS count
                FROM payments
                WHERE status = 'failed'
                  AND COALESCE(payment_created_at, failed_at) >= %s
                GROUP BY COALESCE(failure_category, 'unclassified')
                ORDER BY count DESC, category ASC
                """,
                (start_time,),
            )
            rows = cur.fetchall()

        return [
            FailureReasonStat(category=row[0], count=int(row[1] or 0))
            for row in rows
        ]

    def get_entity_stats(self, hours: int, dimension: str) -> List[EntityStat]:
        """Return payment health grouped by bank or payment method."""
        if dimension not in {"bank", "method"}:
            raise ValueError("dimension must be bank or method")

        start_time = self._get_start_time(hours)
        column = "bank" if dimension == "bank" else "method"

        # `column` is selected exclusively from a hard-coded whitelist above;
        # it is therefore safe to interpolate as an SQL identifier.
        with get_db_cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    {column},
                    COUNT(*) FILTER (WHERE status = 'captured') AS captured,
                    COUNT(*) FILTER (
                        WHERE status = 'failed'
                          AND COALESCE(failure_category, '') <> 'user_cancelled'
                    ) AS failed_effective
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

        results: List[EntityStat] = []
        for value, captured, failed in rows:
            captured_count = int(captured or 0)
            failed_count = int(failed or 0)
            attempts = captured_count + failed_count
            success_rate = (
                captured_count / attempts if attempts else 0.0
            )
            results.append(
                EntityStat(
                    entity=value,
                    attempts=attempts,
                    captured=captured_count,
                    failed=failed_count,
                    success_rate=success_rate,
                )
            )

        return results

    def get_funnel(self, hours: int) -> FunnelStats:
        """Return recovery-funnel and treatment-vs-holdout metrics.

        Payment-level counts are computed from `payments` alone so multiple
        recovery_attempts rows for one payment cannot inflate funnel counts.
        Email delivery count is computed independently from recovery_attempts.

        Rates are ratios in [0, 1], matching the frontend's formatPercent().
        """
        start_time = self._get_start_time(hours)

        with get_db_cursor() as cur:
            # Payment-level metrics: deliberately no JOIN to avoid multiplying
            # one failed payment by multiple recovery attempts.
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (
                        WHERE status = 'failed'
                    ) AS failed,
                    COUNT(*) FILTER (
                        WHERE status = 'failed'
                          AND recovery_group IN ('treatment', 'holdout')
                    ) AS eligible,
                    COUNT(*) FILTER (
                        WHERE status = 'failed'
                          AND recovery_status = 'recovered'
                    ) AS recovered,
                    COUNT(*) FILTER (
                        WHERE status = 'failed'
                          AND recovery_status = 'self_recovered'
                    ) AS self_recovered,
                    COUNT(*) FILTER (
                        WHERE status = 'failed'
                          AND recovery_group = 'treatment'
                    ) AS treatment_count,
                    COUNT(*) FILTER (
                        WHERE status = 'failed'
                          AND recovery_group = 'treatment'
                          AND recovery_status = 'recovered'
                    ) AS treatment_recovered,
                    COUNT(*) FILTER (
                        WHERE status = 'failed'
                          AND recovery_group = 'holdout'
                    ) AS holdout_count,
                    COUNT(*) FILTER (
                        WHERE status = 'failed'
                          AND recovery_group = 'holdout'
                          AND recovery_status = 'self_recovered'
                    ) AS holdout_recovered
                FROM payments
                WHERE COALESCE(payment_created_at, failed_at) >= %s
                """,
                (start_time,),
            )
            payment_row = cur.fetchone()

            # Only successfully sent/recovered email attempts count as emails
            # sent. A failed/unsent attempt must not inflate the funnel.
            cur.execute(
                """
                SELECT COUNT(DISTINCT id)
                FROM recovery_attempts
                WHERE channel = 'email'
                  AND sent_at IS NOT NULL
                  AND status IN ('sent', 'recovered')
                  AND sent_at >= %s
                """,
                (start_time,),
            )
            email_row = cur.fetchone()

        if not payment_row:
            return FunnelStats(
                failed=0,
                eligible=0,
                emails_sent=int(email_row[0] or 0) if email_row else 0,
                recovered=0,
                treatment_count=0,
                treatment_rate=0.0,
                holdout_count=0,
                holdout_rate=0.0,
                self_recovered=0,
            )

        (
            failed,
            eligible,
            recovered,
            self_recovered,
            treatment_count,
            treatment_recovered,
            holdout_count,
            holdout_recovered,
        ) = payment_row

        treatment_count = int(treatment_count or 0)
        holdout_count = int(holdout_count or 0)

        treatment_rate = (
            int(treatment_recovered or 0) / treatment_count
            if treatment_count
            else 0.0
        )
        holdout_rate = (
            int(holdout_recovered or 0) / holdout_count
            if holdout_count
            else 0.0
        )

        return FunnelStats(
            failed=int(failed or 0),
            eligible=int(eligible or 0),
            emails_sent=int(email_row[0] or 0) if email_row else 0,
            recovered=int(recovered or 0),
            treatment_count=treatment_count,
            treatment_rate=treatment_rate,
            holdout_count=holdout_count,
            holdout_rate=holdout_rate,
            self_recovered=int(self_recovered or 0),
        )

    def get_alerts(self) -> List[AlertStat]:
        with get_db_cursor() as cur:
            cur.execute(
                """
                SELECT
                    scope,
                    scope_value,
                    state,
                    last_success_rate,
                    last_baseline,
                    last_attempts,
                    healthy_streak,
                    state_changed_at
                FROM alert_state
                ORDER BY scope, scope_value
                """
            )
            rows = cur.fetchall()

        return [
            AlertStat(
                scope=row[0],
                scope_value=row[1],
                current_success_rate=(
                    float(row[3]) if row[3] is not None else None
                ),
                baseline=(float(row[4]) if row[4] is not None else None),
                attempts=int(row[5] or 0),
                state=row[2],
                state_changed_at=row[7],
            )
            for row in rows
        ]


service = StatsService()


@router.get("/stats/summary", response_model=SummaryStats)
def summary(hours: int = Query(24, ge=1, le=24 * 30)):
    return service.get_summary(hours)


@router.get("/stats/timeseries", response_model=List[TimeSeriesPoint])
def timeseries(hours: int = Query(24, ge=1, le=24 * 30)):
    return service.get_timeseries(hours)


@router.get("/stats/failure-reasons", response_model=List[FailureReasonStat])
def failure_reasons(hours: int = Query(24, ge=1, le=24 * 30)):
    return service.get_failure_reasons(hours)


@router.get("/stats/by-bank", response_model=List[EntityStat])
def by_bank(hours: int = Query(1, ge=1, le=24 * 30)):
    return service.get_entity_stats(hours, "bank")


@router.get("/stats/by-method", response_model=List[EntityStat])
def by_method(hours: int = Query(1, ge=1, le=24 * 30)):
    return service.get_entity_stats(hours, "method")


@router.get("/recovery/funnel", response_model=FunnelStats)
def recovery_funnel(hours: int = Query(24, ge=1, le=24 * 30)):
    return service.get_funnel(hours)


@router.get("/alerts", response_model=List[AlertStat])
def alerts():
    return service.get_alerts()