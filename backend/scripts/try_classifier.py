import os
from backend.app.classifier import classify_failure, ClassificationError
from backend.app.config import settings

def main():
    # 6 sample error sets (one for each category plus one unknown)
    test_cases = [
        {
            "name": "Insufficient Funds",
            "data": ("BAD_REQUEST", "Insufficient funds in account", "bank", "payment"),
            "expected": "insufficient_funds"
        },
        {
            "name": "Bank Declined Soft",
            "data": ("BANK_DECLINED", "Temporary network issue at bank", "bank", "authorization"),
            "expected": "bank_declined_soft"
        },
        {
            "name": "Bank Declined Hard",
            "data": ("FRAUD_DETECTED", "Transaction declined due to fraud risk", "bank", "authorization"),
            "expected": "bank_declined_hard"
        },
        {
            "name": "Timeout",
            "data": ("GATEWAY_TIMEOUT", "The bank took too long to respond", "gateway", "processing"),
            "expected": "timeout"
        },
        {
            "name": "User Cancelled",
            "data": ("USER_CANCELLED", "Customer clicked cancel", "customer", "payment_page"),
            "expected": "user_cancelled"
        },
        {
            "name": "Unknown/Other",
            "data": ("XYZ_123", "Something weird happened", "system", "unknown"),
            "expected": "other"
        },
    ]

    print(f"Testing classifier with model: {settings.GROQ_MODEL_NAME}\n")

    for case in test_cases:
        print(f"--- Case: {case['name']} ---")
        print(f"Input: {case['data']}")
        try:
            result = classify_failure(*case['data'])
            print(f"Result: category={result.category}, confidence={result.confidence}, reason='{result.reason}'")
            print(f"Expected: {case['expected']} -> {'MATCH' if result.category == case['expected'] else 'MISMATCH'}")
        except ClassificationError as e:
            print(f"Error: {e}")
        print()

if __name__ == "__main__":
    main()
