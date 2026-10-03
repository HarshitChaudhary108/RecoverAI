import logging
import time
from datetime import datetime, timedelta
from typing import Tuple, Optional

from backend.app.classifier import classify_failure, ClassificationError
from backend.app.policy import assign_group, get_policy, adjust_for_quiet_hours
from backend.app.db import get_db_cursor
from backend.app.config import settings

logger = logging.getLogger(__name__)

def classify_and_schedule(payment_id: str) -> Tuple[str, Optional[str]]:
    """
    Classifies a payment failure and schedules corresponding recovery actions.
    Returns: (classification_status, category)
    """
    start_time = time.time()
    error_msg = None
    category = None
    confidence = None

    try:
        with get_db_cursor() as cur:
            # 1. Load the payment
            cur.execute(
                "SELECT status, classification_status, error_code, error_reason, "
                "error_source, error_step, failed_at "
                "FROM payments WHERE payment_id = %s FOR UPDATE",
                (payment_id,)
            )
            row = cur.fetchone()
            if not row:
                logger.warning(f"Payment {payment_id} not found")
                return "not_found", None

            status, classification_status, error_code, error_reason, error_source, error_step, failed_at = row

            if classification_status == "classified":
                return "classified", category # Idempotency

            if status == "captured":
                return "skipped", None # Already captured, no actions needed

            # 2. Classify failure
            try:
                classification = classify_failure(error_code, error_reason, error_source, error_step)
                category = classification.category
                confidence = classification.confidence
                reason = classification.reason
            except ClassificationError as e:
                # Use a separate connection to ensure failure state is committed
                # before the exception is re-raised and the main transaction rolls back.
                with get_db_cursor() as fail_cur:
                    fail_cur.execute(
                        "UPDATE payments SET "
                        "classification_status = 'failed', "
                        "classification_error = %s, "
                        "updated_at = %s "
                        "WHERE payment_id = %s",
                        (str(e), datetime.utcnow(), payment_id)
                    )
                    fail_cur.connection.commit()
                raise e

            # 3. Atomic Update: Save classification and schedule actions
            # Update payment status
            cur.execute(
                "UPDATE payments SET "
                "category = %s, "
                "confidence = %s, "
                "classification_reason = %s, "
                "classification_status = 'classified', "
                "updated_at = %s "
                "WHERE payment_id = %s",
                (category, confidence, reason, datetime.utcnow(), payment_id)
            )

            # Assign recovery group
            group = assign_group(payment_id, category)
            cur.execute(
                "UPDATE payments SET recovery_group = %s WHERE payment_id = %s",
                (group, payment_id)
            )

            # Schedule actions if in treatment group
            if group == "treatment":
                policy_steps = get_policy(category)
                for step in policy_steps:
                    if step["action_type"] == "manual_review":
                        continue # handled below for 'other'

                    delay_mins = step.get("delay_minutes", 0)
                    run_at = failed_at + timedelta(minutes=delay_mins)
                    run_at = adjust_for_quiet_hours(run_at)

                    cur.execute(
                        "INSERT INTO scheduled_actions "
                        "(payment_id, action_type, step, run_at, status) "
                        "VALUES (%s, %s, %s, %s, 'pending') "
                        "ON CONFLICT (payment_id, action_type, step) DO NOTHING",
                        (payment_id, step["action_type"], step["step"], run_at)
                    )

            # Special case for 'other' category: manual review
            if category == "other":
                cur.execute(
                    "INSERT INTO scheduled_actions "
                    "(payment_id, action_type, step, run_at, status) "
                    "VALUES (%s, 'manual_review', 1, %s, 'manual_review') "
                    "ON CONFLICT (payment_id, action_type, step) DO NOTHING",
                    (payment_id, datetime.utcnow())
                )
                # Log raw error fields for manual review
                logger.info(f"Manual review required for {payment_id}. Errors: {error_code}, {error_reason}, {error_source}, {error_step}")

            return "classified", category

    except ClassificationError as e:
        error_msg = str(e)
        raise e
    except Exception as e:
        error_msg = str(e)
        logger.exception(f"Unexpected error classifying payment {payment_id}")
        raise e
    finally:
        latency = (time.time() - start_time) * 1000
        # 6. Log one structured line per run
        logger.info(
            "classification_run | payment_id=%s | status=%s | category=%s | "
            "confidence=%s | latency=%.2fms | error=%s",
            payment_id,
            "classified" if category else "failed",
            category,
            confidence,
            latency,
            error_msg
        )
