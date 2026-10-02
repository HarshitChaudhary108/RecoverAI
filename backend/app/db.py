import contextlib
from typing import Generator
import psycopg
from psycopg_pool import ConnectionPool
from backend.app.config import settings

# Initialize the connection pool
# The pool is created at module level to be reused across the application
pool = ConnectionPool(conninfo=settings.DATABASE_URL)

@contextlib.contextmanager
def get_db_cursor() -> Generator[psycopg.Cursor, None, None]:
    """
    Context manager for database transactions.
    - Acquires a connection from the pool.
    - Starts a transaction.
    - Commits on success, rolls back on error.
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            try:
                yield cur
                conn.commit()
            except Exception:
                conn.rollback()
                raise
