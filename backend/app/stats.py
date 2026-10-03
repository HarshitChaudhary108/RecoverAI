from fastapi import APIRouter, Query, HTTPException, Depends
from typing import List, Optional
from datetime import datetime, timedelta, timezone
from backend.app.db import get_db_cursor
from backend.app.schemas import (
    SummaryStats, TimeSeriesPoint, FailureReasonStat,
    EntityStat, FunnelStats, AlertStat
)

class StatsService:
    """
    Service layer for calculating payment health and recovery statistics.
    Encapsulates all SQL logic and data transformation.
    """

    def _get_start_time(self, hours: int) -> datetime:
        """Calculate absolute UTC start time for the given window with a small buffer for clock skew."""
        return datetime.now(timezone.utc) - timedelta(hours=hours, seconds=1)

    def get_summary(self, hours: int) -> SummaryStats:
        start_time = self._get_start_time(hours)
        query = """
            SELECT
                COUNT(*) FILTER (WHERE status = 'captured') * 100.0 /
                    NULLIF(COUNT(*) FILTER (WHERE status <> 'pending' AND COALESCE(failure_category, '') <> 'user_cancelled'), 0) as success_rate,
                COUNT(*) FILTER (WHERE status = 'failed') as failed_count,
                COUNT(*) FILTER (WHERE recovery_status = 'recovered') as recovered_count,
                COALESCE(SUM(amount) FILTER (WHERE recovery_status = 'recovered'), 0) as revenue_recovered,
                COALESCE(SUM(amount) FILTER (WHERE status = 'failed' AND recovery_status = 'open' AND COALESCE(failure_category, '') <> 'user_cancelled'), 0) as revenue_at_risk
            FROM payments
            WHERE (failed_at >= %s OR failed_at IS NULL);
        """
        with get_db_cursor() as cur:
            cur.execute(query, (start_time,))
            row = cur.fetchone()
            if not row:
                return SummaryStats(success_rate=0.0, failed_count=0, recovered_count=0, revenue_recovered=0, revenue_at_risk=0)

            return SummaryStats(
                success_rate=row[0] or 0.0,
                failed_count=row[1] or 0,
                recovered_count=row[2] or 0,
                revenue_recovered=int(row[3] or 0),
                revenue_at_risk=int(row[4] or 0)
            )

    def get_timeseries(self, hours: int) -> List[TimeSeriesPoint]:
        start_time = self._get_start_time(hours)
        query = """
            SELECT ts, success_rate FROM health_snapshots
            WHERE scope = 'global' AND scope_value = 'all' AND ts >= %s
            ORDER BY ts ASC;
        """
        with get_db_cursor() as cur:
            cur.execute(query, (start_time,))
            return [TimeSeriesPoint(timestamp=row[0], success_rate=row[1]) for row in cur.fetchall()]

    def get_failure_reasons(self, hours: int) -> List[FailureReasonStat]:
        start_time = self._get_start_time(hours)
        query = """
            SELECT failure_category, COUNT(*) as count FROM payments
            WHERE status = 'failed' AND failed_at >= %s
            GROUP BY failure_category;
        """
        with get_db_cursor() as cur:
            cur.execute(query, (start_time,))
            return [FailureReasonStat(category=row[0] or 'unknown', count=row[1]) for row in cur.fetchall()]

    def get_entity_stats(self, entity_column: str, hours: int) -> List[EntityStat]:
        # Whitelist allowed columns to prevent SQL injection
        allowed_columns = {'bank', 'method'}
        if entity_column not in allowed_columns:
            raise ValueError(f"Invalid entity column: {entity_column}")

        start_time = self._get_start_time(hours)
        # We use f-string only for the column name which is whitelisted
        query = f"""
            SELECT {entity_column} as entity,
                COUNT(*) as attempts,
                COUNT(*) FILTER (WHERE status = 'captured') as captured,
                COUNT(*) FILTER (WHERE status = 'failed') as failed,
                COUNT(*) FILTER (WHERE status = 'captured') * 100.0 /
                    NULLIF(COUNT(*) FILTER (WHERE status != 'user_cancelled' AND status != 'pending'), 0) as success_rate
            FROM payments
            WHERE (failed_at >= %s OR failed_at IS NULL)
            GROUP BY {entity_column};
        """
        with get_db_cursor() as cur:
            cur.execute(query, (start_time,))
            return [
                EntityStat(
                    entity=row[0] or 'unknown',
                    attempts=row[1],
                    captured=row[2],
                    failed=row[3],
                    success_rate=row[4] or 0.0
                ) for row in cur.fetchall()
            ]

    def get_recovery_funnel(self, hours: int) -> FunnelStats:
        start_time = self._get_start_time(hours)

        # Consolidated query for all payment-based funnel metrics
        # Note: using absolute timestamp check for failed_at
        payment_query = """
            SELECT
                COUNT(*) FILTER (WHERE status = 'failed') as total_failed,
                COUNT(*) FILTER (WHERE status = 'failed' AND recovery_group IN ('treatment', 'holdout')) as eligible,
                COUNT(*) FILTER (WHERE recovery_status = 'recovered') as recovered,
                COUNT(*) FILTER (WHERE recovery_status = 'self_recovered') as self_recovered,
                COUNT(*) FILTER (WHERE status = 'failed' AND recovery_group = 'treatment') as treatment_count,
                COUNT(*) FILTER (WHERE status = 'failed' AND recovery_group = 'treatment' AND recovery_status = 'recovered') * 100.0 /
                    NULLIF(COUNT(*) FILTER (WHERE status = 'failed' AND recovery_group = 'treatment'), 0) as treatment_rate,
                COUNT(*) FILTER (WHERE status = 'failed' AND recovery_group = 'holdout') as holdout_count,
                COUNT(*) FILTER (WHERE status = 'failed' AND recovery_group = 'holdout' AND recovery_status = 'recovered') * 100.0 /
                    NULLIF(COUNT(*) FILTER (WHERE status = 'failed' AND recovery_group = 'holdout'), 0) as holdout_rate
            FROM payments
            WHERE (failed_at >= %s OR failed_at IS NULL);
        """

        # Separate query for email delivery (different table)
        email_query = """
            SELECT COUNT(DISTINCT original_payment_id)
            FROM recovery_attempts
            WHERE sent_at >= %s;
        """

        with get_db_cursor() as cur:
            cur.execute(payment_query, (start_time,))
            p_row = cur.fetchone()

            cur.execute(email_query, (start_time,))
            e_row = cur.fetchone()

            emails_sent = e_row[0] if e_row else 0

            if not p_row:
                return FunnelStats(
                    failed=0, eligible=0, emails_sent=emails_sent, recovered=0,
                    treatment_count=0, treatment_rate=0.0,
                    holdout_count=0, holdout_rate=0.0,
                    self_recovered=0
                )

            return FunnelStats(
                failed=p_row[0] or 0,
                eligible=p_row[1] or 0,
                emails_sent=emails_sent,
                recovered=p_row[2] or 0,
                treatment_count=p_row[4] or 0,
                treatment_rate=p_row[5] or 0.0,
                holdout_count=p_row[6] or 0,
                holdout_rate=p_row[7] or 0.0,
                self_recovered=p_row[3] or 0
            )

    def get_alerts(self) -> List[AlertStat]:
        query = """
            SELECT scope, scope_value, last_success_rate as current_success_rate, last_baseline as baseline,
                   last_attempts as attempts, state, state_changed_at
            FROM alert_state;
        """
        with get_db_cursor() as cur:
            cur.execute(query)
            return [AlertStat(
                scope=row[0], scope_value=row[1], current_success_rate=row[2],
                baseline=row[3], attempts=row[4], state=row[5], state_changed_at=row[6]
            ) for row in cur.fetchall()]

# Dependency for FastAPI
def get_stats_service() -> StatsService:
    return StatsService()

router = APIRouter()

def validate_hours(hours: int):
    if not (1 <= hours <= 168):
        raise HTTPException(status_code=422, detail="Hours must be between 1 and 168")

@router.get("/stats/summary", response_model=SummaryStats)
async def get_summary(hours: int = Query(24), service: StatsService = Depends(get_stats_service)):
    validate_hours(hours)
    return service.get_summary(hours)

@router.get("/stats/timeseries", response_model=List[TimeSeriesPoint])
async def get_timeseries(hours: int = Query(24), service: StatsService = Depends(get_stats_service)):
    validate_hours(hours)
    return service.get_timeseries(hours)

@router.get("/stats/failure-reasons", response_model=List[FailureReasonStat])
async def get_failure_reasons(hours: int = Query(24), service: StatsService = Depends(get_stats_service)):
    validate_hours(hours)
    return service.get_failure_reasons(hours)

@router.get("/stats/by-bank", response_model=List[EntityStat])
async def get_by_bank(hours: int = Query(1), service: StatsService = Depends(get_stats_service)):
    validate_hours(hours)
    return service.get_entity_stats("bank", hours)

@router.get("/stats/by-method", response_model=List[EntityStat])
async def get_by_method(hours: int = Query(1), service: StatsService = Depends(get_stats_service)):
    validate_hours(hours)
    return service.get_entity_stats("method", hours)

@router.get("/recovery/funnel", response_model=FunnelStats)
async def get_funnel(hours: int = Query(24), service: StatsService = Depends(get_stats_service)):
    validate_hours(hours)
    return service.get_recovery_funnel(hours)

@router.get("/alerts", response_model=List[AlertStat])
async def get_alerts(service: StatsService = Depends(get_stats_service)):
    return service.get_alerts()
