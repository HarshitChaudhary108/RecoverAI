import os
import psycopg

TABLES = [
    "payments",
    "scheduled_actions",
    "recovery_attempts",
    "health_snapshots",
    "alert_state",
    "webhook_events",
]


def main() -> None:
    database_url = os.environ["DATABASE_URL"]

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            for table in TABLES:
                print(f"\n=== {table} ===")

                cur.execute(
                    """
                    SELECT column_name, data_type
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = %s
                    ORDER BY ordinal_position
                    """,
                    (table,),
                )

                rows = cur.fetchall()

                if not rows:
                    print("TABLE NOT FOUND")
                    continue

                for column_name, data_type in rows:
                    print(f"{column_name} | {data_type}")


if __name__ == "__main__":
    main()