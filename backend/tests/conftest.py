import pytest
import os
import uuid
from urllib.parse import urlparse
from pathlib import Path

# --- CRITICAL BOOTSTRAP PHASE ---
# We must establish the test database configuration BEFORE any application modules
# (especially backend.app.db) are imported, because backend.app.db initializes
# its connection pool at the module level.

# Read TEST_DATABASE_URL from environment
test_db_url = os.environ.get("TEST_DATABASE_URL")

# Validate that TEST_DATABASE_URL exists
if not test_db_url:
    # We don't pytest.exit here because it would prevent other non-DB tests
    # from running, but we mark it for the fixtures to handle.
    pass

# Validate that it points to a local PostgreSQL host
if test_db_url:
    hostname = urlparse(test_db_url).hostname
    if hostname not in {"localhost", "127.0.0.1", "::1"}:
        # This is a critical safety guard to prevent accidental production writes.
        # We'll let the fixture raise the actual error to be explicit.
        pass

# Set environment variables before importing application settings/db
os.environ["DATABASE_URL"] = test_db_url if test_db_url else ""
# We'll set the schema name here too so it's available immediately
session_schema_name = f"test_recovery_{uuid.uuid4().hex[:8]}"
os.environ["DATABASE_SCHEMA"] = session_schema_name

# Now it is safe to import application modules
from backend.app.config import settings
import psycopg

# --- FIXTURES ---

@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    """
    Fixture to create a temporary schema for tests.
    All tests run within this schema to avoid touching real data.
    """
    db_url = os.environ.get("TEST_DATABASE_URL")

    if not db_url:
        pytest.exit("Tests must be run with TEST_DATABASE_URL environment variable set.")

    hostname = urlparse(db_url).hostname
    if hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.exit(f"Tests must run against a local Postgres. Current TEST_DATABASE_URL host: {hostname}")

    schema_name = os.environ.get("DATABASE_SCHEMA")

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema_name};")
            cur.execute(f"SET search_path TO {schema_name};")

            schema_file = Path(__file__).parent.parent / "schema.sql"
            with open(schema_file, "r") as f:
                sql = f.read()
                cur.execute(sql)
            conn.commit()

    yield schema_name

    # Cleanup
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA {schema_name} CASCADE;")
            conn.commit()

@pytest.fixture
def db_cursor(setup_test_db):
    """
    Fixture providing a connection and cursor for the duration of a test.
    Ensures that all tests roll back their changes to maintain isolation
    and prevent INERROR state.
    """
    schema_name = setup_test_db
    db_url = os.environ.get("TEST_DATABASE_URL")
    conn = psycopg.connect(db_url)
    try:
        conn.execute(f"SET search_path TO {schema_name};")
        cur = conn.cursor()
        yield cur
    finally:
        conn.rollback()
        cur.close()
        conn.close()
