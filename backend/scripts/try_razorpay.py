import sys
import os
# Ensure project root is in path
sys.path.append(os.getcwd())

from backend.app.razorpay_client import RazorpayClient
from backend.app.config import settings

def main():
    print("--- Testing Razorpay Client ---")
    client = RazorpayClient()

    # 1. Try fetch payment
    # Use a dummy ID or a known test ID from your dashboard
    test_payment_id = "pay_test_123"
    print(f"Fetching payment {test_payment_id}...")
    try:
        payment = client.fetch_payment(test_payment_id)
        print(f"Success: {payment}")
    except Exception as e:
        print(f"Expected error for dummy ID: {e}")

    # 2. Try create link
    print("\nCreating test payment link...")
    try:
        link_id, link_url = client.create_payment_link(
            amount=10000, # 100 INR
            currency="INR",
            customer_email="test-link@example.com",
            description="Test Recovery Link",
            action_id="act_test_001",
            original_payment_id="pay_test_123",
            expiry_unix=1735689600 # Fixed date in future
        )
        print(f"Success! Link ID: {link_id}, URL: {link_url}")
    except Exception as e:
        print(f"Error creating link: {e}")

if __name__ == "__main__":
    main()
