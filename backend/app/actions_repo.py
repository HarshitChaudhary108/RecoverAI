from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from backend.app.db import get_db_cursor

class ClaimError(Exception):
    """Raised when an action is operated on without a valid active claim."""
    pass

class ActionsRepository:
    LEASE_TIME = timedelta(minutes=5)

    @staticmethod
    def generate_action_id(payment_id: str, action_type: str, step: int) -> str:
        """Generates a deterministic action identifier based on business identity."""
        return f"act_{payment_id}_{action_type}_{step}"

    def claim_due_actions(self, worker_id: str, limit: int, cur=None) -> List[Dict[str, Any]]:
        """
        Claims due actions in a single atomic transaction.
        An action is due if it's pending and run_at <= now,
        or if it was claimed but the lease has expired.
        """
        if cur is not None:
            # Use provided cursor
            cur.execute(
                """
                UPDATE scheduled_actions
                SET status = 'claimed',
                    locked_by = %s,
                    lease_expires_at = NOW() + %s,
                    attempts = attempts + 1
                WHERE action_id IN (
                    SELECT action_id
                    FROM scheduled_actions
                    WHERE (status = 'pending' AND run_at <= NOW())
                       OR (status = 'claimed' AND lease_expires_at < NOW())
                    ORDER BY run_at ASC
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING action_id, payment_id, action_type, step, run_at, status, attempts, locked_by, lease_expires_at
                """,
                (worker_id, self.LEASE_TIME, limit)
            )
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]
        else:
            # Open a new transaction
            with get_db_cursor() as internal_cur:
                internal_cur.execute(
                    """
                    UPDATE scheduled_actions
                    SET status = 'claimed',
                        locked_by = %s,
                        lease_expires_at = NOW() + %s,
                        attempts = attempts + 1
                    WHERE action_id IN (
                        SELECT action_id
                        FROM scheduled_actions
                        WHERE (status = 'pending' AND run_at <= NOW())
                           OR (status = 'claimed' AND lease_expires_at < NOW())
                        ORDER BY run_at ASC
                        LIMIT %s
                        FOR UPDATE SKIP LOCKED
                    )
                    RETURNING action_id, payment_id, action_type, step, run_at, status, attempts, locked_by, lease_expires_at
                    """,
                    (worker_id, self.LEASE_TIME, limit)
                )
                columns = [desc[0] for desc in internal_cur.description]
                return [dict(zip(columns, row)) for row in internal_cur.fetchall()]

    def still_owns_claim(self, action_id: str, worker_id: str) -> bool:
        """True if action is claimed by worker_id and lease is still valid."""
        with get_db_cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM scheduled_actions
                WHERE action_id = %s
                  AND locked_by = %s
                  AND status = 'claimed'
                  AND lease_expires_at > NOW()
                """,
                (action_id, worker_id)
            )
            return cur.fetchone() is not None

    def complete_action(self, action_id: str, worker_id: str):
        """Marks action as completed. Requires active ownership."""
        if not self.still_owns_claim(action_id, worker_id):
            raise ClaimError(f"Worker {worker_id} does not own an active claim for action {action_id}")

        with get_db_cursor() as cur:
            cur.execute(
                "UPDATE scheduled_actions SET status = 'completed', completed_at = NOW() WHERE action_id = %s",
                (action_id,)
            )

    def cancel_action(self, action_id: str, worker_id: str, reason: str):
        """Cancels action. Requires active ownership."""
        if not self.still_owns_claim(action_id, worker_id):
            raise ClaimError(f"Worker {worker_id} does not own an active claim for action {action_id}")

        with get_db_cursor() as cur:
            cur.execute(
                "UPDATE scheduled_actions SET status = 'cancelled', result = %s WHERE action_id = %s",
                (reason, action_id)
            )

    def reschedule_action(self, action_id: str, worker_id: str, new_run_at: datetime, error: str):
        """Reschedules action for future run. Requires active ownership."""
        if not self.still_owns_claim(action_id, worker_id):
            raise ClaimError(f"Worker {worker_id} does not own an active claim for action {action_id}")

        with get_db_cursor() as cur:
            cur.execute(
                "UPDATE scheduled_actions SET status = 'pending', run_at = %s, last_error = %s WHERE action_id = %s",
                (new_run_at, error, action_id)
            )

    def fail_action(self, action_id: str, worker_id: str, error: str):
        """Marks action as failed. Requires active ownership."""
        if not self.still_owns_claim(action_id, worker_id):
            raise ClaimError(f"Worker {worker_id} does not own an active claim for action {action_id}")

        with get_db_cursor() as cur:
            cur.execute(
                "UPDATE scheduled_actions SET status = 'failed', last_error = %s WHERE action_id = %s",
                (error, action_id)
            )

    def get_action_and_payment(self, action_id: str) -> tuple[Dict[str, Any], Dict[str, Any]]:
        """Fetches the action and its associated payment in one go."""
        with get_db_cursor() as cur:
            cur.execute(
                """
                SELECT a.*, p.payment_id, p.amount, p.currency, p.customer_email, p.failure_category, p.failed_at, p.recovery_group
                FROM scheduled_actions a
                JOIN payments p ON a.payment_id = p.payment_id
                WHERE a.action_id = %s
                """,
                (action_id,)
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Action {action_id} not found")

            columns = [desc[0] for desc in cur.description]
            data = dict(zip(columns, row))

            # Split into action and payment dicts
            action_cols = {"action_id", "payment_id", "action_type", "step", "run_at", "status", "attempts", "locked_by", "lease_expires_at", "result", "last_error", "completed_at"}
            payment_cols = {"payment_id", "amount", "currency", "customer_email", "failure_category", "failed_at", "recovery_group"}

            action = {k: v for k, v in data.items() if k in action_cols}
            payment = {k: v for k, v in data.items() if k in payment_cols}

            # Ensure 'email' alias exists for backward compatibility with executor.py
            if "customer_email" in payment:
                payment["email"] = payment["customer_email"]

            return action, payment

    def complete_action_cur(self, cur, action_id: str, now):
        """Marks action as completed using provided cursor."""
        cur.execute(
            "UPDATE scheduled_actions SET status = 'completed', completed_at = %s WHERE action_id = %s",
            (now, action_id)
        )

    def cancel_action_cur(self, cur, action_id: str, reason: str):
        """Cancels action using provided cursor."""
        cur.execute(
            "UPDATE scheduled_actions SET status = 'cancelled', result = %s WHERE action_id = %s",
            (reason, action_id)
        )

    def reschedule_action_cur(self, cur, action_id: str, new_run_at: datetime, error: str):
        """Reschedules action using provided cursor."""
        cur.execute(
            "UPDATE scheduled_actions SET status = 'pending', run_at = %s, last_error = %s WHERE action_id = %s",
            (new_run_at, error, action_id)
        )

    def fail_action_cur(self, cur, action_id: str, error: str):
        """Marks action as failed using provided cursor."""
        cur.execute(
            "UPDATE scheduled_actions SET status = 'failed', last_error = %s WHERE action_id = %s",
            (error, action_id)
        )
