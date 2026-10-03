import contextlib
from typing import Generator
import psycopg
from psycopg_pool import ConnectionPool
from backend.app.config import settings
import os

# Initialize the connection pool
# The pool is created at module level to be reused across the application
pool = ConnectionPool(conninfo=settings.DATABASE_URL)

@contextlib.contextmanager
def get_db_cursor() -> Generator[psycopg.Cursor, None, None]:
    """
    Context manager for database transactions.
    - Acquires a connection from the pool.
    - Sets the search path for the session.
    - Starts a transaction.
    - Commits on success, rolls back on error.
    """
    with pool.connection() as conn:
        # Ensure we are using the correct schema (especially important for tests)
        schema = os.getenv("DATABASE_SCHEMA", "public")
        conn.execute(f"SET search_path TO {schema}")

        with conn.cursor() as cur:
            try:
                yield cur
                conn.commit()
            except Exception:
                conn.rollback()
                raise
