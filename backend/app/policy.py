import hashlib
from datetime import datetime, timedelta, time
from typing import List, Dict, Optional, Any
from zoneinfo import ZoneInfo

from backend.app.config import settings

def get_policy(category: str) -> List[Dict[str, Any]]:
    """
    Returns the recovery steps for a given failure category.
    Each step contains action_type, step number, and delay in minutes.
    """
    # Mapping from spec.md Section 7
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

    # Special case for 'other' or unknown categories which have no delays but need a step
    if category == "other" or category not in settings.RECOVERY_DELAYS:
        return [{"action_type": action_type, "step": 1, "delay_minutes": 0}]

    return [
        {"action_type": action_type, "step": i + 1, "delay_minutes": delay}
        for i, delay in enumerate(delays)
    ]

def adjust_for_quiet_hours(utc_time: datetime) -> datetime:
    """
    Adjusts a UTC time to respect quiet hours (21:00 to 09:00 Asia/Kolkata).
    If it falls within quiet hours, it is moved to the next 09:00 IST.
    """
    if not settings.QUIET_HOURS_ENABLED:
        return utc_time

    tz = ZoneInfo(settings.QUIET_HOURS_TIMEZONE)
    ist_time = utc_time.astimezone(tz)

    start_h = int(settings.QUIET_HOURS_START.split(":")[0])
    end_h = int(settings.QUIET_HOURS_END.split(":")[0])

    current_h = ist_time.hour

    # Quiet hours are 21:00 to 09:00.
    # This means if hour >= 21 OR hour < 9, we are in quiet hours.
    if current_h >= start_h or current_h < end_h:
        if current_h >= start_h:
            # Move to 09:00 tomorrow
            target_date = ist_time.date() + timedelta(days=1)
        else:
            # Move to 09:00 today
            target_date = ist_time.date()

        adjusted_ist = datetime.combine(target_date, time(end_h, 0))
        return adjusted_ist.astimezone(ZoneInfo("UTC"))

    return utc_time

def is_expired(failed_at: datetime, now: datetime) -> bool:
    """True if the payment failed more than RECOVERY_EXPIRY_DAYS ago."""
    return (now - failed_at).days >= settings.RECOVERY_EXPIRY_DAYS

def can_send_more(messages_already_sent: int) -> bool:
    """True if we haven't reached the MAX_MESSAGES_PER_PAYMENT limit."""
    return messages_already_sent < settings.MAX_MESSAGES_PER_PAYMENT

def assign_group(payment_id: str, category: str) -> str:
    """
    Assigns a payment to treatment, holdout, or not_eligible.
    Uses a stable hash of payment_id for consistency.
    """
    if category not in settings.HOLDOUT_ELIGIBLE_CATEGORIES:
        return "not_eligible"

    # Stable hash for grouping
    hash_digest = hashlib.sha256(payment_id.encode()).hexdigest()
    hash_int = int(hash_digest, 16)
    bucket = hash_int % 100

    if bucket < settings.HOLDOUT_PERCENT:
        return "holdout"
    return "treatment"

def suggests_other_method(category: str, action_type: str) -> bool:
    """True for categories where suggesting a different method is recommended."""
    return category in ["insufficient_funds", "bank_declined_hard"]

def next_timeout_recheck(now: datetime, recheck_number: int) -> Optional[datetime]:
    """Returns the next time to re-check a timeout payment, or None if max reached."""
    if recheck_number >= settings.MAX_RECHECKS:
        return None
    return now + timedelta(minutes=settings.TIMEOUT_RECHECK_DELAY_MINUTES)

def retry_backoff(attempt: int) -> int:
    """Returns a delay in seconds that grows with each attempt, capped at the last configured factor."""
    factors = settings.RETRY_BACKOFF_FACTORS
    if attempt < 0:
        return factors[0]
    if attempt >= len(factors):
        return factors[-1]
    return factors[attempt]
