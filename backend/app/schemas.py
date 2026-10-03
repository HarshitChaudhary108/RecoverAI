from pydantic import BaseModel
from datetime import datetime
from typing import Optional

class SummaryStats(BaseModel):
    success_rate: Optional[float]
    failed_count: int
    recovered_count: int
    revenue_recovered: int
    revenue_at_risk: int

class TimeSeriesPoint(BaseModel):
    timestamp: datetime
    success_rate: Optional[float]

class FailureReasonStat(BaseModel):
    category: str
    count: int

class EntityStat(BaseModel):
    entity: str
    attempts: int
    captured: int
    failed: int
    success_rate: Optional[float]

class FunnelStats(BaseModel):
    failed: int
    eligible: int
    emails_sent: int
    recovered: int
    treatment_count: int
    treatment_rate: Optional[float]
    holdout_count: int
    holdout_rate: Optional[float]
    self_recovered: int

class AlertStat(BaseModel):
    scope: str
    scope_value: str
    current_success_rate: Optional[float]
    baseline: Optional[float]
    attempts: int
    state: str
    state_changed_at: datetime
