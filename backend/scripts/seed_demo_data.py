import argparse
import random
from datetime import datetime, timedelta
import psycopg
from backend.app.db import pool

def seed_data(clear: bool = False):
    with pool.connection() as conn:
        with conn.cursor() as cur:
            if clear:
                print("Clearing existing demo data...")
                cur.execute("TRUNCATE payments, scheduled_actions, recovery_attempts, health_snapshots, alert_state CASCADE;")

            print("Generating 7 days of demo data...")

            banks = ["Chase", "Bank of America", "Wells Fargo", "Citi", "HSBC"]
            methods = ["Credit Card", "ACH", "Apple Pay", "Google Pay"]
            categories = ["insufficient_funds", "bank_declined_soft", "bank_declined_hard", "timeout", "user_cancelled", "other"]
            groups = ["treatment", "holdout", "not_eligible"]

            now = datetime.utcnow()

            # Generate hourly data for 7 days
            for hour_offset in range(7 * 24):
                ts = now - timedelta(hours=hour_offset)

                # 1. Generate Payments
                for _ in range(random.randint(80, 120)):
                    payment_id = f"demo_pay_{hour_offset}_{_}"
                    order_id = f"demo_ord_{hour_offset}_{_}"
                    amount = random.randint(1000, 10000)
                    bank = random.choice(banks)
                    method = random.choice(methods)

                    # Random status
                    rand = random.random()
                    if rand < 0.7:
                        status = 'captured'
                        captured_at = ts
                        failed_at = None
                        cat = None
                        group = None
                        rec_status = 'captured'
                    elif rand < 0.9:
                        status = 'failed'
                        captured_at = None
                        failed_at = ts
                        cat = random.choice(categories)
                        group = random.choice(groups)
                        rec_status = 'open'
                        if group == 'not_eligible':
                            rec_status = 'not_eligible'
                    else:
                        status = 'failed'
                        captured_at = None
                        failed_at = ts
                        cat = 'user_cancelled'
                        group = None
                        rec_status = 'open'

                    # Randomly resolve some failed payments to 'recovered' or 'self_recovered'
                    if status == 'failed' and rec_status == 'open':
                        res_rand = random.random()
                        if res_rand < 0.2:
                            rec_status = 'recovered'
                        elif res_rand < 0.3:
                            rec_status = 'self_recovered'

                    cur.execute("""
                        INSERT INTO payments (
                            payment_id, order_id, amount, currency, method, bank, status,
                            customer_email, payment_created_at, failed_at, captured_at,
                            failure_category, recovery_group, recovery_status, updated_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        payment_id, order_id, amount, 'INR', method, bank, status,
                        f"user_{_}@example.com", ts, failed_at, captured_at, cat, group, rec_status, ts
                    ))

                    # 2. Create Recovery Attempts for Treatment Group
                    if status == 'failed' and group == 'treatment':
                        # Only some treatment payments get emails
                        if random.random() < 0.8:
                            attempt_id = f"demo_att_{payment_id}"
                            cur.execute("""
                                INSERT INTO recovery_attempts (
                                    id, original_payment_id, channel, recovery_group, delay_used, sent_at, status
                                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                            """, (
                                attempt_id, payment_id, 'email', 'treatment', '2m', ts + timedelta(minutes=5), 'sent'
                            ))

                # 3. Health Snapshots
                # Global
                cur.execute("""
                    INSERT INTO health_snapshots (ts, scope, scope_value, attempts, captured, failed, success_rate)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (ts, 'global', 'all', 100, 75, 20, 75.0))

                # Per Bank
                for bank in banks:
                    cur.execute("""
                        INSERT INTO health_snapshots (ts, scope, scope_value, attempts, captured, failed, success_rate)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (ts, 'bank', bank, 50, 35, 10, 70.0))

            # 4. Alert State (One active alert)
            cur.execute("""
                INSERT INTO alert_state (scope, scope_value, state, healthy_streak, last_success_rate, last_baseline, last_attempts, state_changed_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                'bank', 'Chase', 'alerting', 0, 62.5, 75.0, 40, now - timedelta(hours=2), now
            ))

            conn.commit()
            print("Demo data seeded successfully!")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--clear", action="store_true", help="Clear existing demo data before seeding")
    args = parser.parse_args()
    seed_data(args.clear)
