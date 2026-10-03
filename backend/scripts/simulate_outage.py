import argparse
import logging
import uuid
from datetime import datetime
from backend.app.db import get_db_cursor

# Setup logging to match project style
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def simulate_outage(bank_name: str, count: int):
    """
    Inserts fake failed payments for a specific bank to simulate an outage.
    Payments start with 'sim_', have no email, and are category 'other'.
    """
    logger.info(f"Simulating outage for bank: {bank_name} with {count} failures...")

    with get_db_cursor() as cur:
        for i in range(count):
            payment_id = f"sim_{uuid.uuid4()}"
            order_id = f"sim_order_{uuid.uuid4()}"

            cur.execute("""
                INSERT INTO payments (
                    payment_id, order_id, amount, currency, method, bank, status,
                    failure_category, classification_status, recovery_status, payment_created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                payment_id, order_id, 1000, 'INR', 'card', bank_name, 'failed',
                'other', 'classified', 'open', datetime.utcnow()
            ))

    logger.info(f"Successfully inserted {count} simulation payments for {bank_name}.")

def clear_simulations():
    """Deletes all simulated payments."""
    logger.info("Clearing simulation data...")
    with get_db_cursor() as cur:
        cur.execute("DELETE FROM payments WHERE payment_id LIKE 'sim_%'")
    logger.info("Simulation data cleared.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simulate payment outages for health monitoring tests.")
    parser.add_argument("--bank", type=str, help="Bank name to simulate outage for")
    parser.add_argument("--count", type=int, default=10, help="Number of failed payments to insert")
    parser.add_argument("--clear", action="store_true", help="Clear all simulation data")

    args = parser.parse_args()

    if args.clear:
        clear_simulations()
    elif args.bank:
        simulate_outage(args.bank, args.count)
    else:
        parser.print_help()
