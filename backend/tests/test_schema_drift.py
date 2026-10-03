import pytest
import re
import os
from psycopg import errors
from backend.tests.conftest import db_cursor

def get_all_sql_queries():
    """Extracts all SQL queries from backend/app and backend/worker."""
    queries = []
    # Regex to find common SQL patterns in strings
    sql_pattern = re.compile(
        r"(INSERT\s+INTO|UPDATE|SELECT|DELETE)\s+.*?(?=\",\s*|\',\s*|\)|$)",
        re.IGNORECASE | re.DOTALL
    )

    # We search for patterns that look like SQL queries within quotes
    # This is a heuristic; we'll refine it by executing against the DB
    for root, _, files in os.walk("backend"):
        if "tests" in root:
            continue
        for file in files:
            if file.endswith(".py"):
                path = os.path.join(root, file)
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                    # Find everything inside quotes that looks like SQL
                    # This is tricky, but since we are executing them,
                    # we can just extract likely candidates.
                    matches = re.finditer(r"(['\"])(.*?)\1", content, re.DOTALL)
                    for match in matches:
                        query = match.group(2)
                        if any(kw in query.upper() for kw in ["INSERT INTO", "UPDATE", "SELECT", "DELETE"]):
                            queries.append(query)
    return queries

def test_schema_drift(db_cursor):
    """
    Executes every SQL query found in the codebase against the test schema.
    If any query references a non-existent column or table, it will fail.
    """
    queries = get_all_sql_queries()
    assert len(queries) > 0, "No SQL queries found to verify"

    failed_queries = []
    for query in queries:
        try:
            # Use EXPLAIN to verify the query without actually modifying data
            # or requiring full parameters.
            # For INSERT/UPDATE, EXPLAIN usually works if the syntax is valid.
            # Since these queries often have %s, we replace them with dummy values
            # or just use a very simple replacement to make it syntactically valid for EXPLAIN.

            # Replace %s with dummy values for EXPLAIN to work
            explain_query = query
            while "%s" in explain_query:
                explain_query = explain_query.replace("%s", "'drift_test'", 1)

            # Wrap in EXPLAIN to avoid side effects
            db_cursor.execute(f"EXPLAIN {explain_query}")
        except (errors.UndefinedColumn, errors.UndefinedTable) as e:
            failed_queries.append((query, str(e)))
        except Exception:
            # Other errors (syntax, etc.) are ignored as the regex might pick up
            # non-executable fragments, but UndefinedColumn/Table are critical.
            pass

    if failed_queries:
        report = "\n\n".join([f"Query: {q}\nError: {e}" for q, e in failed_queries])
        pytest.fail(f"Schema drift detected! The following queries reference missing columns/tables:\n{report}")
