import pytest
from backend.app.config import settings

def test_config_loads_defaults():
    # Testing that the settings object is instantiated and has expected defaults
    assert settings.MAX_MESSAGES_PER_PAYMENT == 3
    assert settings.RECOVERY_EXPIRY_DAYS == 7
    assert settings.QUIET_HOURS_ENABLED is True

def test_config_infrastructure_keys_present():
    # These are required in the Settings class.
    # In a real test environment, we'd mock the .env file or environment variables.
    # For this skeleton test, we just check if the settings object has these attributes.
    assert hasattr(settings, "RAZORPAY_KEY_ID")
    assert hasattr(settings, "DATABASE_URL")
    assert hasattr(settings, "GROQ_API_KEY")
