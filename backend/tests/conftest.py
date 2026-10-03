import pytest
import psycopg
import os
import uuid
from urllib.parse import urlparse
from backend.app.config import settings
from pathlib import Path

@pytest.fixture(scope="session", autouse=True)
def setup_test_db(monkeypatch):
    """
    Fixture to create a temporary schema for tests.
    All tests run within this schema to avoid touching real data.
    """
    # Refuse to run if the URL host is not localhost/127.0.0.1
    db_url = settings.TEST_DATABASE_URL
    hostname = urlparse(db_url).hostname
    if hostname not in {"localhost", "127.0.0.1"}:
        pytest.exit(f"Tests must run against a local Postgres. Current TEST_DATABASE_URL host: {hostname}")

    # Monkeypatch settings.DATABASE_URL to TEST_DATABASE_URL so that the
    # app code (e.g. get_db_cursor() using the pool) uses the test DB.
    monkeypatch.setattr(settings, "DATABASE_URL", settings.TEST_DATABASE_URL)

    # Give each session a unique schema name to avoid collisions
    schema_name = f"test_recovery_{uuid.uuid4().hex[:8]}"

    # Set the environment variable so that get_db_cursor() uses this schema
    os.environ["DATABASE_SCHEMA"] = schema_name

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema_name};")
            cur.execute(f"SET search_path TO {schema_name};")

            schema_file = Path(__file__).parent.parent / "schema.sql"
            with open(schema_file, "r") as f:
                sql = f.read()
                cur.execute(sql)
            conn.commit()
        # Ensure we don't drop it until the session ends
    yield schema_name

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
    conn = psycopg.connect(settings.TEST_DATABASE_URL)
    try:
        conn.execute(f"SET search_path TO {schema_name};")
        cur = conn.cursor()
        yield cur
    finally:
        conn.rollback()
        cur.close()
        conn.close()
