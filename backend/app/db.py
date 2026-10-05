import contextlib
import os
import threading
from typing import Generator, Optional

import psycopg
from psycopg import sql
from psycopg_pool import ConnectionPool

from backend.app.config import settings

_pool: Optional[ConnectionPool] = None
_pool_pid: Optional[int] = None
_pool_lock = threading.Lock()


def _get_pool() -> ConnectionPool:
    """Create the PostgreSQL pool lazily inside the current process.

    Psycopg connections/pools must not be shared across a fork boundary. Lazy,
    process-local construction keeps Celery prefork workers from inheriting a
    live connection pool created by the parent process.
    """
    global _pool, _pool_pid

    pid = os.getpid()
    if _pool is not None and _pool_pid == pid:
        return _pool

    with _pool_lock:
        if _pool is None or _pool_pid != pid:
            _pool = ConnectionPool(
                conninfo=settings.DATABASE_URL,
                min_size=1,
                max_size=4,
                timeout=10,
                check=ConnectionPool.check_connection,
            )
            _pool_pid = pid

    return _pool


def _database_schema() -> str:
    schema = os.getenv("DATABASE_SCHEMA", "public")
    if not schema:
        return "public"
    return schema


@contextlib.contextmanager
def get_db_cursor() -> Generator[psycopg.Cursor, None, None]:
    pool = _get_pool()
    schema = _database_schema()

    with pool.connection() as conn:
        conn.execute(
            sql.SQL("SET search_path TO {}")
            .format(sql.Identifier(schema))
        )
        with conn.cursor() as cur:
            try:
                yield cur
                conn.commit()
            except Exception:
                conn.rollback()
                raise


def close_db_pool() -> None:
    """Close the current process-local pool during application shutdown."""
    global _pool, _pool_pid
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None
            _pool_pid = None
