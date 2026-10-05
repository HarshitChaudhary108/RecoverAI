from pathlib import Path
import os

import psycopg

MIGRATION = Path("backend/migrations/001_production_alignment.sql")

def main() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    database_url = os.environ["DATABASE_URL"]

    with psycopg.connect(database_url) as conn:
        conn.execute(sql)
        conn.commit()

    print("Migration applied successfully.")


if __name__ == "__main__":
    main()