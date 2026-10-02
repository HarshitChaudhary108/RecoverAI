import sys
import os
# Ensure project root is in path
sys.path.append(os.getcwd())

from backend.app.messaging import send_recovery_email
from backend.app.config import settings

def main():
    print("--- Testing Messaging Client ---")

    # Use your own account email for Resend onboarding test
    test_email = "your-email@example.com" # CHANGE THIS to your verified email

    print(f"Sending test recovery email to {test_email}...")
    try:
        send_recovery_email(
            to=test_email,
            amount=50000, # 500 INR
            currency="INR",
            link_url="https://rzp.io/i/test_link",
            kind="recovery",
            suggest_other_method=True,
            idempotency_key="test-idemp-001"
        )
        print("Success: Email sent!")
    except Exception as e:
        print(f"Error sending email: {e}")

if __name__ == "__main__":
    main()
