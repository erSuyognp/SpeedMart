"""Opt-in: make ONE real Stripe TEST-mode charge of $1.00 with the test Visa (pm_card_visa).

    python scripts/stripe_smoke.py            asks you to type "yes" first
    python scripts/stripe_smoke.py --yes      no prompt

Reads STRIPE_SECRET_KEY from .env and refuses anything that is not an sk_test_ key, so no real money can move.
Does not touch the SpeedMart database. The charge shows up at https://dashboard.stripe.com/test/payments
with description "SpeedMart smoke test". Exit code 0 on success.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
AMOUNT_CENTS = 100


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One $1.00 Stripe test-mode charge.")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    parser.add_argument("--env", default=str(ROOT / ".env"), help="env file (default: .env)")
    args = parser.parse_args(argv)

    key = (dotenv_values(args.env).get("STRIPE_SECRET_KEY") or "").strip()
    if not key.startswith("sk_test_"):
        print("STRIPE_SECRET_KEY in .env is missing or not a TEST key (sk_test_...). Nothing was charged.",
              file=sys.stderr)
        return 2
    if not args.yes:
        answer = input("Create one $1.00 Stripe TEST-mode charge now? Type yes: ").strip().lower()
        if answer != "yes":
            print("Cancelled. Nothing was charged.")
            return 1

    import stripe

    stripe.api_key = key
    stripe.default_http_client = stripe.HTTPXClient(timeout=15, allow_sync_methods=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        customer = stripe.Customer.create(name="SpeedMart smoke test", metadata={"source": "stripe_smoke.py"})
        pm = stripe.PaymentMethod.attach("pm_card_visa", customer=customer.id)
        intent = stripe.PaymentIntent.create(
            amount=AMOUNT_CENTS, currency="usd", customer=customer.id, payment_method=pm.id,
            payment_method_types=["card"], off_session=True, confirm=True,
            description="SpeedMart smoke test", metadata={"source": "stripe_smoke.py", "run": stamp},
            idempotency_key=f"speedmart-smoke-{stamp}",
        )
    except stripe.StripeError as e:
        print(f"Stripe error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(f"customer        {customer.id}")
    print(f"payment method  {pm.id}")
    print(f"payment intent  {intent.id}")
    print(f"status          {intent.status}")
    print(f"amount          ${intent.amount / 100:.2f} {intent.currency.upper()}")
    print(f"auth code (app) {intent.id[-6:].upper()}")
    print(f"dashboard       https://dashboard.stripe.com/test/payments/{intent.id}")
    return 0 if intent.status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
