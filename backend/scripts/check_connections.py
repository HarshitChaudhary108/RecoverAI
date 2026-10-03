import os
import sys
from dotenv import load_dotenv
import psycopg
import redis
from urllib.parse import urlparse

def redact_url(text):
    # Simple redaction for URLs in error messages
    import re
    # Matches things that look like postgresql://... or redis://...
    pattern = r'(postgresql://|redis://|rediss://)[^ ]+'
    return re.sub(pattern, '***', text)

def check():
    load_dotenv()

    print("Checking connections...")

    # Postgres
    try:
        db_url = os.getenv("DATABASE_URL")
        if not db_url:
            print("Postgres: FAILED (DATABASE_URL missing)")
        else:
            with psycopg.connect(db_url, connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    print("Postgres: OK")
    except Exception as e:
        msg = str(e).split('\n')[0]
        print(f"Postgres: FAILED ({type(e).__name__}: {redact_url(msg)})")

    # Redis
    try:
        redis_url = os.getenv("REDIS_URL")
        if not redis_url:
            print("Redis: FAILED (REDIS_URL missing)")
        else:
            r = redis.from_url(redis_url, socket_timeout=5)
            if r.ping():
                print("Redis: OK")
    except Exception as e:
        msg = str(e).split('\n')[0]
        print(f"Redis: FAILED ({type(e).__name__}: {redact_url(msg)})")

if __name__ == "__main__":
    check()
