import pytest
import psycopg
from backend.app.config import settings

@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    """
    Fixture to create a temporary schema for tests.
    All tests run within this schema to avoid touching real data.
    """
    # We use a random schema name to avoid collisions during parallel tests
    schema_name = "test_recovery_agent_schema"

    # Connect and create schema
    with psycopg.connect(settings.DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema_name};")
            cur.execute(f"SET search_path TO {schema_name};")

            # Apply schema.sql
            schema_file = Path(__file__).parent.parent / "schema.sql"
            with open(schema_file, "r") as f:
                sql = f.read()
                cur.execute(sql)
            conn.commit()

    yield schema_name

    # Cleanup: Drop the schema
    with psycopg.connect(settings.DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA {schema_name} CASCADE;")
            conn.commit()

@pytest.fixture
def db_cursor(setup_test_db):
    """
    Fixture providing a cursor tied to the test schema.
    """
    schema_name = setup_test_db
    conn = psycopg.connect(settings.DATABASE_URL)
    # Ensure we are operating in the test schema
    conn.execute(f"SET search_path TO {schema_name};")
    cur = conn.cursor()
    yield cur
    conn.commit()  # Commit changes before closing
    cur.close()
    conn.close()

from pathlib import Path
