from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from backend.app.config import settings
from backend.app.db import get_db_cursor


class ClaimError(Exception):
    """Raised when a worker no longer owns a claimed scheduled action."""


class ActionsRepository:
    """PostgreSQL-backed scheduled-action repository with lease-safe transitions."""

    def __init__(self) -> None:
        self.lease_time = timedelta(seconds=settings.LEASE_TIME_SECONDS)

    def claim_due_actions(
        self,
        worker_id: str,
        limit: int,
        cur=None,
    ) -> List[Dict[str, Any]]:
        """Atomically claim due or expired actions using SKIP LOCKED."""
        if limit <= 0:
            return []

        statement = """
            WITH candidates AS (
                SELECT action_id
                FROM scheduled_actions
                WHERE (
                    status = 'pending'
                    AND run_at <= NOW()
                )
                OR (
                    status = 'claimed'
                    AND (lease_expires_at IS NULL OR lease_expires_at <= NOW())
                )
                ORDER BY run_at ASC, action_id ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            UPDATE scheduled_actions AS a
            SET status = 'claimed',
                locked_by = %s,
                lease_expires_at = NOW() + %s,
                attempts = a.attempts + 1,
                last_error = NULL
            FROM candidates AS c
            WHERE a.action_id = c.action_id
            RETURNING
                a.action_id,
                a.payment_id,
                a.action_type,
                a.step,
                a.run_at,
                a.status,
                a.result,
                a.attempts,
                a.recheck_count,
                a.locked_by,
                a.lease_expires_at,
                a.last_error,
                a.completed_at;
        """
        params = (limit, worker_id, self.lease_time)

        if cur is not None:
            cur.execute(statement, params)
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]

        with get_db_cursor() as internal_cur:
            internal_cur.execute(statement, params)
            columns = [desc[0] for desc in internal_cur.description]
            return [dict(zip(columns, row)) for row in internal_cur.fetchall()]

    def still_owns_claim(self, action_id: str, worker_id: str) -> bool:
        with get_db_cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM scheduled_actions
                WHERE action_id = %s
                  AND locked_by = %s
                  AND status = 'claimed'
                  AND lease_expires_at > NOW()
                """,
                (action_id, worker_id),
            )
            return cur.fetchone() is not None

    def renew_claim(self, action_id: str, worker_id: str) -> bool:
        """Extend an active lease only for its current owner."""
        with get_db_cursor() as cur:
            cur.execute(
                """
                UPDATE scheduled_actions
                SET lease_expires_at = NOW() + %s
                WHERE action_id = %s
                  AND locked_by = %s
                  AND status = 'claimed'
                  AND lease_expires_at > NOW()
                RETURNING action_id
                """,
                (self.lease_time, action_id, worker_id),
            )
            return cur.fetchone() is not None

    @staticmethod
    def _transition_claimed_cur(
        cur,
        action_id: str,
        worker_id: str,
        *,
        status: str,
        result: Optional[str] = None,
        run_at: Optional[datetime] = None,
        last_error: Optional[str] = None,
        completed: bool = False,
        increment_recheck: bool = False,
    ) -> bool:
        """Compare-and-set transition requiring the current worker's live lease."""
        set_clauses = [
            "status = %s",
            "locked_by = NULL",
            "lease_expires_at = NULL",
        ]
        params: List[Any] = [status]

        if result is not None:
            set_clauses.append("result = %s")
            params.append(result)

        if run_at is not None:
            normalized = (
                run_at.replace(tzinfo=timezone.utc)
                if run_at.tzinfo is None
                else run_at.astimezone(timezone.utc)
            )
            set_clauses.append("run_at = %s")
            params.append(normalized)
            set_clauses.append("completed_at = NULL")

        if last_error is not None:
            set_clauses.append("last_error = %s")
            params.append(last_error[:4000])

        if completed:
            set_clauses.append("completed_at = CURRENT_TIMESTAMP")

        if increment_recheck:
            set_clauses.append("recheck_count = recheck_count + 1")

        params.extend([action_id, worker_id])
        cur.execute(
            f"""
            UPDATE scheduled_actions
            SET {', '.join(set_clauses)}
            WHERE action_id = %s
              AND locked_by = %s
              AND status = 'claimed'
              AND lease_expires_at > NOW()
            RETURNING action_id
            """,
            tuple(params),
        )
        return cur.fetchone() is not None

    def complete_action(self, action_id: str, worker_id: str) -> bool:
        with get_db_cursor() as cur:
            return self.complete_action_cur(cur, action_id, worker_id)

    def cancel_action(self, action_id: str, worker_id: str, reason: str) -> bool:
        with get_db_cursor() as cur:
            return self.cancel_action_cur(cur, action_id, worker_id, reason)

    def reschedule_action(
        self,
        action_id: str,
        worker_id: str,
        new_run_at: datetime,
        error: str,
        *,
        increment_recheck: bool = False,
    ) -> bool:
        with get_db_cursor() as cur:
            return self.reschedule_action_cur(
                cur,
                action_id,
                worker_id,
                new_run_at,
                error,
                increment_recheck=increment_recheck,
            )

    def fail_action(self, action_id: str, worker_id: str, error: str) -> bool:
        with get_db_cursor() as cur:
            return self.fail_action_cur(cur, action_id, worker_id, error)

    def get_action_and_payment(
        self,
        action_id: str,
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        """Load all fields needed by the executor from the canonical schema."""
        with get_db_cursor() as cur:
            cur.execute(
                """
                SELECT
                    a.action_id,
                    a.payment_id,
                    a.action_type,
                    a.step,
                    a.run_at,
                    a.status,
                    a.result,
                    a.attempts,
                    a.recheck_count,
                    a.locked_by,
                    a.lease_expires_at,
                    a.last_error,
                    a.created_at,
                    a.completed_at,
                    p.payment_id AS p_payment_id,
                    p.order_id,
                    p.amount,
                    p.currency,
                    p.customer_email AS email,
                    p.customer_contact,
                    p.failure_category,
                    p.failed_at,
                    p.recovery_group,
                    p.recovery_status
                FROM scheduled_actions AS a
                JOIN payments AS p
                  ON p.payment_id = a.payment_id
                WHERE a.action_id = %s
                """,
                (action_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Action {action_id} not found")

            columns = [desc[0] for desc in cur.description]
            data = dict(zip(columns, row))
            data["payment_id"] = data.pop("p_payment_id")

            action_keys = {
                "action_id",
                "payment_id",
                "action_type",
                "step",
                "run_at",
                "status",
                "result",
                "attempts",
                "recheck_count",
                "locked_by",
                "lease_expires_at",
                "last_error",
                "created_at",
                "completed_at",
            }
            payment_keys = {
                "payment_id",
                "order_id",
                "amount",
                "currency",
                "email",
                "customer_contact",
                "failure_category",
                "failed_at",
                "recovery_group",
                "recovery_status",
            }
            action = {k: data[k] for k in action_keys if k in data}
            payment = {k: data[k] for k in payment_keys if k in data}
            return action, payment

    def complete_action_cur(self, cur, action_id: str, worker_id: str) -> bool:
        return self._transition_claimed_cur(
            cur,
            action_id,
            worker_id,
            status="completed",
            completed=True,
        )

    def cancel_action_cur(self, cur, action_id: str, worker_id: str, reason: str) -> bool:
        return self._transition_claimed_cur(
            cur,
            action_id,
            worker_id,
            status="cancelled",
            result=reason,
            completed=True,
        )

    def reschedule_action_cur(
        self,
        cur,
        action_id: str,
        worker_id: str,
        new_run_at: datetime,
        error: str,
        *,
        increment_recheck: bool = False,
    ) -> bool:
        return self._transition_claimed_cur(
            cur,
            action_id,
            worker_id,
            status="pending",
            run_at=new_run_at,
            last_error=error,
            increment_recheck=increment_recheck,
        )

    def fail_action_cur(self, cur, action_id: str, worker_id: str, error: str) -> bool:
        return self._transition_claimed_cur(
            cur,
            action_id,
            worker_id,
            status="failed",
            last_error=error,
            completed=True,
        )
