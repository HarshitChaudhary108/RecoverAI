import pytest
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from unittest.mock import patch

from backend.app.policy import (
    get_policy,
    adjust_for_quiet_hours,
    is_expired,
    can_send_more,
    assign_group,
    suggests_other_method,
    next_timeout_recheck,
    retry_backoff,
)
from backend.app.config import settings

# --- Quiet Hours Tests ---

def test_adjust_for_quiet_hours_outside():
    # 15:00 IST (15:00 - 5:30 = 09:30 UTC)
    utc_time = datetime(2026, 10, 3, 9, 30, tzinfo=ZoneInfo("UTC"))
    # Should remain unchanged
    result = adjust_for_quiet_hours(utc_time)
    assert result == utc_time

def test_adjust_for_quiet_hours_sharp_start():
    # 21:00 IST (21:00 - 5:30 = 15:30 UTC)
    utc_time = datetime(2026, 10, 3, 15, 30, tzinfo=ZoneInfo("UTC"))
    # Should move to 09:00 IST tomorrow
    # Tomorrow 09:00 IST = Oct 4, 03:30 UTC
    expected = datetime(2026, 10, 4, 3, 30, tzinfo=ZoneInfo("UTC"))
    result = adjust_for_quiet_hours(utc_time)
    assert result == expected

def test_adjust_for_quiet_hours_sharp_end():
    # 09:00 IST (09:00 - 5:30 = 03:30 UTC)
    utc_time = datetime(2026, 10, 3, 3, 30, tzinfo=ZoneInfo("UTC"))
    # Should remain unchanged
    result = adjust_for_quiet_hours(utc_time)
    assert result == utc_time

def test_adjust_for_quiet_hours_just_before_end():
    # 08:59 IST (08:59 - 5:30 = 03:29 UTC)
    utc_time = datetime(2026, 10, 3, 3, 29, tzinfo=ZoneInfo("UTC"))
    # Should move to 09:00 IST today (03:30 UTC)
    expected = datetime(2026, 10, 3, 3, 30, tzinfo=ZoneInfo("UTC"))
    result = adjust_for_quiet_hours(utc_time)
    assert result == expected

def test_adjust_for_quiet_hours_middle_of_night():
    # 00:30 IST (00:30 - 5:30 = 19:00 UTC previous day)
    utc_time = datetime(2026, 10, 2, 19, 0, tzinfo=ZoneInfo("UTC"))
    # Should move to 09:00 IST today (Oct 3, 03:30 UTC)
    expected = datetime(2026, 10, 3, 3, 30, tzinfo=ZoneInfo("UTC"))
    result = adjust_for_quiet_hours(utc_time)
    assert result == expected

def test_adjust_for_quiet_hours_late_night():
    # 23:59 IST (23:59 - 5:30 = 18:29 UTC)
    utc_time = datetime(2026, 10, 3, 18, 29, tzinfo=ZoneInfo("UTC"))
    # Should move to 09:00 IST tomorrow (Oct 4, 03:30 UTC)
    expected = datetime(2026, 10, 4, 3, 30, tzinfo=ZoneInfo("UTC"))
    result = adjust_for_quiet_hours(utc_time)
    assert result == expected

def test_adjust_for_quiet_hours_disabled():
    # 21:00 IST (15:30 UTC)
    utc_time = datetime(2026, 10, 3, 15, 30, tzinfo=ZoneInfo("UTC"))
    with patch("backend.app.policy.settings") as mock_settings:
        mock_settings.QUIET_HOURS_ENABLED = False
        result = adjust_for_quiet_hours(utc_time)
        assert result == utc_time

# --- Policy Mapping Tests ---

@pytest.mark.parametrize("category,expected_action", [
    ("insufficient_funds", "send_recovery_email"),
    ("bank_declined_soft", "send_recovery_email"),
    ("bank_declined_hard", "send_recovery_email"),
    ("timeout", "send_recovery_email"),
    ("user_cancelled", "send_email_reminder"),
    ("other", "manual_review"),
    ("unknown_cat", "manual_review"),
])
def test_get_policy_action_mapping(category, expected_action):
    policy = get_policy(category)
    assert policy[0]["action_type"] == expected_action

def test_get_policy_delays():
    # bank_declined_soft has [20, 120]
    policy = get_policy("bank_declined_soft")
    assert len(policy) == 2
    assert policy[0]["delay_minutes"] == 20
    assert policy[1]["delay_minutes"] == 120
    assert policy[0]["step"] == 1
    assert policy[1]["step"] == 2

def test_get_policy_other():
    policy = get_policy("other")
    assert len(policy) == 1
    assert policy[0]["action_type"] == "manual_review"
    assert policy[0]["delay_minutes"] == 0

# --- Guardrail Tests ---

def test_is_expired():
    now = datetime(2026, 10, 10, tzinfo=ZoneInfo("UTC"))

    # Exactly 7 days ago -> Expired (per spec: "true after 7 days" and policy "true if >= 7")
    failed_7 = now - timedelta(days=7)
    assert is_expired(failed_7, now) is True

    # 6 days 23 hours ago -> Not expired
    failed_6_23 = now - timedelta(days=6, hours=23)
    assert is_expired(failed_6_23, now) is False

def test_can_send_more():
    assert can_send_more(0) is True
    assert can_send_more(1) is True
    assert can_send_more(2) is True
    assert can_send_more(3) is False

# --- Grouping Tests ---

def test_assign_group_consistency():
    payment_id = "pay_12345"
    category = "insufficient_funds"
    group1 = assign_group(payment_id, category)
    group2 = assign_group(payment_id, category)
    assert group1 == group2

def test_assign_group_eligibility():
    # "other" is not in HOLDOUT_ELIGIBLE_CATEGORIES
    assert assign_group("pay_123", "other") == "not_eligible"

def test_assign_group_distribution():
    # Sample 1000 random IDs
    ids = [f"pay_{i}" for i in range(1000)]
    category = "insufficient_funds"
    results = [assign_group(pid, category) for pid in ids]

    holdout_count = results.count("holdout")
    # Expected 10% = 100. Allow +/- 30 for variance in small sample
    assert 70 <= holdout_count <= 130

def test_assign_group_config_change():
    payment_id = "pay_fixed_id" # Found via trial to be treatment at 10%
    category = "insufficient_funds"

    with patch("backend.app.policy.settings") as mock_settings:
        mock_settings.HOLDOUT_ELIGIBLE_CATEGORIES = settings.HOLDOUT_ELIGIBLE_CATEGORIES
        mock_settings.HOLDOUT_PERCENT = 0
        assert assign_group(payment_id, category) == "treatment"

        mock_settings.HOLDOUT_PERCENT = 100
        assert assign_group(payment_id, category) == "holdout"

# --- Utility Tests ---

def test_suggests_other_method():
    assert suggests_other_method("insufficient_funds", "send_recovery_email") is True
    assert suggests_other_method("bank_declined_hard", "send_recovery_email") is True
    assert suggests_other_method("timeout", "send_recovery_email") is False

def test_next_timeout_recheck():
    now = datetime(2026, 10, 3, 10, 0, tzinfo=ZoneInfo("UTC"))

    # Attempt 0
    next_t = next_timeout_recheck(now, 0)
    assert next_t == now + timedelta(minutes=settings.TIMEOUT_RECHECK_DELAY_MINUTES)

    # Max attempt
    next_t_max = next_timeout_recheck(now, settings.MAX_RECHECKS)
    assert next_t_max is None

def test_retry_backoff():
    # Factors: [60, 300, 1800]
    assert retry_backoff(0) == 60
    assert retry_backoff(1) == 300
    assert retry_backoff(2) == 1800
    assert retry_backoff(3) == 1800 # Cap
    assert retry_backoff(-1) == 60   # Floor
