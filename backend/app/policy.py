import hashlib
from datetime import datetime, timedelta, time, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from backend.app.config import settings

CATEGORY_ACTIONS = {
    "insufficient_funds": "send_recovery_email",
    "bank_declined_soft": "send_recovery_email",
    "bank_declined_hard": "send_recovery_email",
    "timeout": "send_recovery_email",
    "user_cancelled": "send_email_reminder",
    "other": "manual_review",
}


def get_policy(category: str) -> List[Dict[str, Any]]:
    """Return only deterministic, configured recovery steps."""
    action_type = CATEGORY_ACTIONS.get(category, "manual_review")
    delays = settings.RECOVERY_DELAYS.get(category)
    if delays is None:
        return [{"action_type": "manual_review", "step": 1, "delay_minutes": 0}]
    if category == "other":
        return [{"action_type": action_type, "step": 1, "delay_minutes": 0}]
    return [
        {"action_type": action_type, "step": i + 1, "delay_minutes": int(delay)}
        for i, delay in enumerate(delays)
    ]


def delay_for_step(category: Optional[str], action_type: str, step: int) -> int:
    if not category:
        return 0
    steps = get_policy(category)
    for configured in steps:
        if configured["action_type"] == action_type and configured["step"] == step:
            return int(configured["delay_minutes"])
    return 0


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def adjust_for_quiet_hours(utc_time: datetime) -> datetime:
    """Move a scheduled UTC instant to the next allowed local time."""
    utc_time = _ensure_utc(utc_time)
    if not settings.QUIET_HOURS_ENABLED:
        return utc_time

    tz = ZoneInfo(settings.QUIET_HOURS_TIMEZONE)
    local = utc_time.astimezone(tz)
    start_hour, start_minute = map(int, settings.QUIET_HOURS_START.split(":"))
    end_hour, end_minute = map(int, settings.QUIET_HOURS_END.split(":"))
    quiet_start = time(start_hour, start_minute)
    quiet_end = time(end_hour, end_minute)
    local_clock = local.time().replace(tzinfo=None)

    overnight = quiet_start > quiet_end
    in_quiet = (
        local_clock >= quiet_start or local_clock < quiet_end
        if overnight
        else quiet_start <= local_clock < quiet_end
    )
    if not in_quiet:
        return utc_time

    if overnight and local_clock >= quiet_start:
        target_date = local.date() + timedelta(days=1)
    else:
        target_date = local.date()

    return datetime.combine(target_date, quiet_end, tzinfo=tz).astimezone(timezone.utc)


def is_expired(failed_at: Optional[datetime], now: datetime) -> bool:
    if failed_at is None:
        return False
    return _ensure_utc(now) >= _ensure_utc(failed_at) + timedelta(
        days=settings.RECOVERY_EXPIRY_DAYS
    )


def can_send_more(messages_already_sent: int) -> bool:
    return messages_already_sent < settings.MAX_MESSAGES_PER_PAYMENT


def assign_group(payment_id: str, category: str) -> str:
    """Stable treatment/holdout assignment using the configured percentage."""
    if category not in settings.HOLDOUT_ELIGIBLE_CATEGORIES:
        return "not_eligible"
    if settings.HOLDOUT_PERCENT <= 0:
        return "treatment"
    if settings.HOLDOUT_PERCENT >= 100:
        return "holdout"
    bucket = int(hashlib.sha256(payment_id.encode("utf-8")).hexdigest(), 16) % 100
    return "holdout" if bucket < settings.HOLDOUT_PERCENT else "treatment"


def suggests_other_method(category: Optional[str], action_type: str) -> bool:
    return category in {"insufficient_funds", "bank_declined_hard"}


def next_timeout_recheck(now: datetime, recheck_number: int) -> Optional[datetime]:
    if recheck_number >= settings.MAX_RECHECKS:
        return None
    return _ensure_utc(now) + timedelta(
        minutes=settings.TIMEOUT_RECHECK_DELAY_MINUTES
    )


def retry_backoff(attempt: int) -> int:
    factors = settings.RETRY_BACKOFF_FACTORS
    if not factors:
        return 60
    index = max(0, min(int(attempt), len(factors) - 1))
    return int(factors[index])
