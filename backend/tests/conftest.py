import pytest
import psycopg
from backend.app.config import settings
from pathlib import Path

@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    """
    Fixture to create a temporary schema for tests.
    All tests run within this schema to avoid touching real data.
    """
    schema_name = "test_recovery_agent_schema"

    with psycopg.connect(settings.DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema_name};")
            cur.execute(f"SET search_path TO {schema_name};")

            schema_file = Path(__file__).parent.parent / "schema.sql"
            with open(schema_file, "r") as f:
                sql = f.read()
                cur.execute(sql)
            conn.commit()

    yield schema_name

    with psycopg.connect(settings.DATABASE_URL) as conn:
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
    conn = psycopg.connect(settings.DATABASE_URL)
    try:
        conn.execute(f"SET search_path TO {schema_name};")
        cur = conn.cursor()
        yield cur
    finally:
        conn.rollback()
        cur.close()
        conn.close()
