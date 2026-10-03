import os
from pathlib import Path
from typing import Dict, List
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Use the path to the backend directory where the .env actually resides
ENV_FILE_PATH = Path(__file__).parent.parent / ".env"

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE_PATH,
        env_file_encoding='utf-8',
        extra='ignore'
    )

    # --- Infrastructure Secrets (Section 18) ---
    RAZORPAY_KEY_ID: str
    RAZORPAY_KEY_SECRET: str
    RAZORPAY_WEBHOOK_SECRET: str
    DATABASE_URL: str
    TEST_DATABASE_URL: str
    # No default REDIS_URL. Pydantic will raise a ValidationError if missing.
    REDIS_URL: str
    NGROK_AUTHTOKEN: str
    EMAIL_API_KEY: str
    GROQ_API_KEY: str
    GROQ_MODEL_NAME: str = "openai/gpt-oss-120b"









    # LangSmith Observability
    LANGCHAIN_TRACING_V2: bool = True
    LANGCHAIN_ENDPOINT: str = "https://api.smith.langchain.com"
    LANGCHAIN_API_KEY: str = ""
    LANGCHAIN_PROJECT: str = "razorpay_failed_payment_recovery_agent"

    # --- Business Settings ---
    RECOVERY_DELAYS: Dict[str, List[int]] = {
        "insufficient_funds": [2],
        "bank_declined_soft": [20, 120],
        "bank_declined_hard": [5],
        "timeout": [10],
        "user_cancelled": [180],
        "other": [],
    }

    MAX_MESSAGES_PER_PAYMENT: int = 3
    QUIET_HOURS_START: str = "21:00"
    QUIET_HOURS_END: str = "09:00"
    QUIET_HOURS_TIMEZONE: str = "Asia/Kolkata"
    QUIET_HOURS_ENABLED: bool = True
    RECOVERY_EXPIRY_DAYS: int = 7
    RECOVERY_ATTRIBUTION_WINDOW_HOURS: int = 48
    HOLDOUT_PERCENT: int = 10
    HOLDOUT_ELIGIBLE_CATEGORIES: List[str] = [
        "insufficient_funds",
        "bank_declined_soft",
        "bank_declined_hard",
        "timeout",
        "user_cancelled"
    ]
    HEALTH_WINDOW_MINUTES: int = 60
    ALERT_MIN_ATTEMPTS: int = 20
    ALERT_SUCCESS_FLOOR_PERCENT: float = 70.0
    ALERT_DROP_THRESHOLD_PERCENT: float = 15.0
    ALERT_BASELINE_DAYS: int = 7
    ALERT_HEALTHY_CHECKS_TO_CLEAR: int = 2
    SCHEDULER_INTERVAL_SECONDS: int = 60
    LEASE_TIME_SECONDS: int = 300
    RETRY_BACKOFF_FACTORS: List[int] = [60, 300, 1800]
    TIMEOUT_RECHECK_DELAY_MINUTES: int = 10
    MAX_RECHECKS: int = 6
    MAX_CLASSIFICATION_RETRIES: int = 3

settings = Settings()
