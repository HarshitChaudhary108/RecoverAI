import psycopg
from backend.app.config import settings

def init_db():
    print("Initializing database from schema.sql...")
    schema_path = Path(__file__).parent.parent / "schema.sql"

    with open(schema_path, "r") as f:
        sql = f.read()

    try:
        # Connect to the database and apply the schema
        with psycopg.connect(settings.DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                conn.commit()
        print("Database initialized successfully.")
    except Exception as e:
        print(f"Error initializing database: {e}")
        sys.exit(1)

if __name__ == "__main__":
    init_db()
