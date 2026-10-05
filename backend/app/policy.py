import hashlib
from datetime import datetime, timedelta, time, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from backend.app.config import settings


def get_policy(category: str) -> List[Dict[str, Any]]:
    """Return deterministic recovery steps for a classified failure."""
    category_action_map = {
        "insufficient_funds": "send_recovery_email",
        "bank_declined_soft": "send_recovery_email",
        "bank_declined_hard": "send_recovery_email",
        "timeout": "send_recovery_email",
        "user_cancelled": "send_email_reminder",
        "other": "manual_review",
    }
    action_type = category_action_map.get(category, "manual_review")
    delays = settings.RECOVERY_DELAYS.get(category, [])

    if category == "other" or category not in settings.RECOVERY_DELAYS:
        return [{"action_type": action_type, "step": 1, "delay_minutes": 0}]

    return [
        {"action_type": action_type, "step": i + 1, "delay_minutes": delay}
        for i, delay in enumerate(delays)
    ]


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def adjust_for_quiet_hours(utc_time: datetime) -> datetime:
    """Move a UTC time into the next allowed 09:00 Asia/Kolkata slot when needed."""
    utc_time = _ensure_utc(utc_time)
    if not settings.QUIET_HOURS_ENABLED:
        return utc_time

    tz = ZoneInfo(settings.QUIET_HOURS_TIMEZONE)
    local_time = utc_time.astimezone(tz)
    start_hour, start_minute = map(int, settings.QUIET_HOURS_START.split(":"))
    end_hour, end_minute = map(int, settings.QUIET_HOURS_END.split(":"))

    local_clock = local_time.time().replace(tzinfo=None)
    quiet_start = time(start_hour, start_minute)
    quiet_end = time(end_hour, end_minute)

    in_overnight_quiet = (
        local_clock >= quiet_start or local_clock < quiet_end
    ) if quiet_start > quiet_end else (
        quiet_start <= local_clock < quiet_end
    )

    if not in_overnight_quiet:
        return utc_time

    if quiet_start > quiet_end and local_clock >= quiet_start:
        target_date = local_time.date() + timedelta(days=1)
    else:
        target_date = local_time.date()

    adjusted_local = datetime.combine(
        target_date,
        quiet_end,
        tzinfo=tz,
    )
    return adjusted_local.astimezone(timezone.utc)


def is_expired(failed_at: Optional[datetime], now: datetime) -> bool:
    """Return True when the configured recovery lifetime has elapsed."""
    if failed_at is None:
        return False
    return _ensure_utc(now) >= _ensure_utc(failed_at) + timedelta(
        days=settings.RECOVERY_EXPIRY_DAYS
    )


def can_send_more(messages_already_sent: int) -> bool:
    return messages_already_sent < settings.MAX_MESSAGES_PER_PAYMENT


def assign_group(payment_id: str, category: str) -> str:
    """Stable treatment/holdout assignment for eligible categories."""
    if category not in settings.HOLDOUT_ELIGIBLE_CATEGORIES:
        return "not_eligible"

    hash_int = int(hashlib.sha256(payment_id.encode("utf-8")).hexdigest(), 16)
    return "holdout" if hash_int % 100 < settings.HOLDOUT_PERCENT else "treatment"


def suggests_other_method(category: str, action_type: str) -> bool:
    return category in {"insufficient_funds", "bank_declined_hard"}


def next_timeout_recheck(now: datetime, recheck_number: int) -> Optional[datetime]:
    if recheck_number >= settings.MAX_RECHECKS:
        return None
    return _ensure_utc(now) + timedelta(minutes=settings.TIMEOUT_RECHECK_DELAY_MINUTES)


def retry_backoff(attempt: int) -> int:
    factors = settings.RETRY_BACKOFF_FACTORS
    if not factors:
        return 60
    index = max(0, min(attempt, len(factors) - 1))
    return factors[index]
