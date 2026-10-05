from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from backend.app.config import settings
from backend.app.db import get_db_cursor


class ClaimError(Exception):
    """Raised when a worker no longer owns a claimed action."""


class ActionsRepository:
    def __init__(self) -> None:
        self.lease_time = timedelta(seconds=settings.LEASE_TIME_SECONDS)

    def claim_due_actions(
        self,
        worker_id: str,
        limit: int,
        cur=None,
    ) -> List[Dict[str, Any]]:
        if limit <= 0:
            return []

        statement = """
            WITH candidates AS (
                SELECT action_id
                FROM scheduled_actions
                WHERE (status = 'pending' AND run_at <= NOW())
                   OR (status = 'claimed' AND lease_expires_at < NOW())
                ORDER BY run_at ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            UPDATE scheduled_actions AS a
            SET status = 'claimed',
                locked_by = %s,
                lease_expires_at = NOW() + %s,
                attempts = a.attempts + 1
            FROM candidates AS c
            WHERE a.action_id = c.action_id
            RETURNING a.action_id, a.payment_id, a.action_type, a.step,
                      a.run_at, a.status, a.attempts, a.recheck_count,
                      a.locked_by, a.lease_expires_at;
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
        set_clauses = ["status = %s", "locked_by = NULL", "lease_expires_at = NULL"]
        params: List[Any] = [status]

        if result is not None:
            set_clauses.append("result = %s")
            params.append(result)
        if run_at is not None:
            set_clauses.append("run_at = %s")
            params.append(run_at)
        if last_error is not None:
            set_clauses.append("last_error = %s")
            params.append(last_error)
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

    def complete_action(self, action_id: str, worker_id: str) -> None:
        with get_db_cursor() as cur:
            if not self._transition_claimed_cur(
                cur, action_id, worker_id, status="completed", completed=True
            ):
                raise ClaimError(
                    f"Worker {worker_id} no longer owns action {action_id}"
                )

    def cancel_action(self, action_id: str, worker_id: str, reason: str) -> None:
        with get_db_cursor() as cur:
            if not self._transition_claimed_cur(
                cur, action_id, worker_id, status="cancelled", result=reason, completed=True
            ):
                raise ClaimError(
                    f"Worker {worker_id} no longer owns action {action_id}"
                )

    def reschedule_action(
        self,
        action_id: str,
        worker_id: str,
        new_run_at: datetime,
        error: str,
        *,
        increment_recheck: bool = False,
    ) -> None:
        if new_run_at.tzinfo is None:
            new_run_at = new_run_at.replace(tzinfo=timezone.utc)

        with get_db_cursor() as cur:
            if not self._transition_claimed_cur(
                cur,
                action_id,
                worker_id,
                status="pending",
                run_at=new_run_at,
                last_error=error,
                increment_recheck=increment_recheck,
            ):
                raise ClaimError(
                    f"Worker {worker_id} no longer owns action {action_id}"
                )

    def fail_action(self, action_id: str, worker_id: str, error: str) -> None:
        with get_db_cursor() as cur:
            if not self._transition_claimed_cur(
                cur, action_id, worker_id, status="failed", last_error=error, completed=True
            ):
                raise ClaimError(
                    f"Worker {worker_id} no longer owns action {action_id}"
                )

    def get_action_and_payment(
        self, action_id: str
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
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
                    a.attempts,
                    a.recheck_count,
                    a.locked_by,
                    a.lease_expires_at,
                    a.result,
                    a.last_error,
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
                JOIN payments AS p ON p.payment_id = a.payment_id
                WHERE a.action_id = %s
                """,
                (action_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Action {action_id} not found")

            columns = [desc[0] for desc in cur.description]
            data = dict(zip(columns, row))

            action_keys = {
                "action_id",
                "payment_id",
                "action_type",
                "step",
                "run_at",
                "status",
                "attempts",
                "recheck_count",
                "locked_by",
                "lease_expires_at",
                "result",
                "last_error",
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

            data["payment_id"] = data.pop("p_payment_id")
            action = {k: data[k] for k in action_keys if k in data}
            payment = {k: data[k] for k in payment_keys if k in data}
            return action, payment

    def complete_action_cur(self, cur, action_id: str, worker_id: str, now: datetime) -> None:
        if not self._transition_claimed_cur(
            cur, action_id, worker_id, status="completed", completed=True
        ):
            raise ClaimError(
                f"Worker {worker_id} no longer owns action {action_id}"
            )

    def cancel_action_cur(self, cur, action_id: str, worker_id: str, reason: str) -> None:
        if not self._transition_claimed_cur(
            cur, action_id, worker_id, status="cancelled", result=reason, completed=True
        ):
            raise ClaimError(
                f"Worker {worker_id} no longer owns action {action_id}"
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
    ) -> None:
        if not self._transition_claimed_cur(
            cur,
            action_id,
            worker_id,
            status="pending",
            run_at=new_run_at,
            last_error=error,
            increment_recheck=increment_recheck,
        ):
            raise ClaimError(
                f"Worker {worker_id} no longer owns action {action_id}"
            )

    def fail_action_cur(self, cur, action_id: str, worker_id: str, error: str) -> None:
        if not self._transition_claimed_cur(
            cur, action_id, worker_id, status="failed", last_error=error, completed=True
        ):
            raise ClaimError(
                f"Worker {worker_id} no longer owns action {action_id}"
            )
