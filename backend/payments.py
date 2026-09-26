"""VIC-shaped instruction (8.6), charge (9.9), payment records (8.7), loyalty (9.10).

The instruction is a sandbox object modeled on Visa Intelligent Commerce concepts; nothing here calls Visa.
Charges go to Stripe **test mode** when F10 is on and STRIPE_SECRET_KEY is an sk_test_ key (S4.2), else to the
mock provider. Any Stripe failure other than a card decline falls back to mock so the demo continues.
Written against stripe-python 15.6.1 (legacy resource API: stripe.Customer.create etc., stripe.CardError).
"""

from __future__ import annotations

import json
import math
import secrets
import sys
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import stripe

from backend import cart as cart_mod
from backend import db, eventlog
from backend.settings import settings

AGENT = {"id": "speedmart-shelf-agent-01", "name": "SpeedMart Store Agent"}
INSTRUCTION_LABEL = "SANDBOX: structure modeled on Visa Intelligent Commerce concepts. Not a Visa API call."
INSTRUCTION_TTL = timedelta(minutes=15)
# Shown for every member: Stripe test mode attaches pm_card_visa; the mock provider stands in for it otherwise.
DEFAULT_CARD_LABEL = "Visa •••• 4242 (test)"
FORCED_DECLINE_MESSAGE = "Card declined (sandbox: forced by staff)"

AUTHORIZED, DECLINED, ERROR = "AUTHORIZED", "DECLINED", "ERROR"
TEST_PAYMENT_METHOD = "pm_card_visa"  # Stripe's test Visa (4242), no card entry UI needed
STRIPE_TIMEOUT_S = 8  # a hung Stripe call must not freeze the exit gate; failure -> mock fallback

_lock = threading.Lock()  # one charge at a time: a double tap must never charge twice
_pending: dict[str, dict[str, Any]] = {}  # session id -> instruction issued at quote time


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def payments_log_path():
    return db.DATA_DIR / "payments.log.jsonl"


# --- instruction (8.6) ---

def build_instruction(session_id: str, cart: dict[str, Any], member: dict[str, Any],
                      now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    count = sum(i["qty"] for i in cart["items"])
    store_name = settings.store["name"]
    return {
        "instruction_id": db.new_id("instr"),
        "label": INSTRUCTION_LABEL,
        "agent": dict(AGENT),
        "agent_token": {
            "token_ref": f"tok_speedmart_{session_id}",
            "scope": {
                "merchant": store_name,
                "max_amount_usd": float(member["budget_usd"]),
                "currency": settings.store.get("currency", "USD"),
                "single_use": True,
                "expires_at": _iso(now + INSTRUCTION_TTL),
            },
        },
        "user_intent": f"Pay ${cart['total_usd']:.2f} to {store_name} for {count} item{'s' if count != 1 else ''}",
        "items": [{"sku": i["sku"], "qty": i["qty"], "unit_price_usd": i["unit_price_usd"]} for i in cart["items"]],
        "amount_usd": cart["total_usd"],
        "cardholder_confirmation": {"method": "passkey" if settings.features.passkeys else "confirm_button",
                                    "verified_at": None},  # filled in when the shopper approves
        "created_at": _iso(now),
    }


def instruction_for(session_id: str, cart: dict[str, Any], member: dict[str, Any]) -> dict[str, Any]:
    """The instruction for this checkout: reused while it matches the frozen cart and has not expired."""
    with _lock:
        instr = _pending.get(session_id)
        if (instr is None or instr["amount_usd"] != cart["total_usd"]
                or instr["agent_token"]["scope"]["max_amount_usd"] != float(member["budget_usd"])
                or _parse(instr["agent_token"]["scope"]["expires_at"]) <= datetime.now(timezone.utc)):
            instr = build_instruction(session_id, cart, member)
            _pending[session_id] = instr
            eventlog.log("instruction_issued", session_id=session_id, instruction_id=instr["instruction_id"],
                         amount_usd=instr["amount_usd"], max_amount_usd=instr["agent_token"]["scope"]["max_amount_usd"])
        return json.loads(json.dumps(instr))  # a copy: callers fill in cardholder_confirmation


def forget_instruction(session_id: str) -> None:
    with _lock:
        _pending.pop(session_id, None)


def over_scope(cart: dict[str, Any], instruction: dict[str, Any]) -> bool:
    return cart_mod.to_cents(cart["total_usd"]) > cart_mod.to_cents(instruction["agent_token"]["scope"]["max_amount_usd"])


# --- Stripe test mode (F10, 9.9) ---

def stripe_key_error(features=None, key: str | None = None) -> str | None:
    """Why the configured key must be refused, or None. Only sk_test_ keys are ever used."""
    features = features or settings.features
    key = settings.env.stripe_secret_key if key is None else key
    if features.stripe and key and not key.startswith("sk_test_"):
        return ("STRIPE_SECRET_KEY must be a Stripe TEST key (sk_test_...). Refusing to start with any other key; "
                "remove it or set features.stripe to false.")
    return None


def _refuse_non_test_key() -> None:
    """9.9: refuse to start with a live / restricted / wrong key. An empty key just means mock payments."""
    error = stripe_key_error()
    if error:
        print(f"\n[payments] Startup aborted: {error}\n", file=sys.stderr)
        raise SystemExit(1)


_refuse_non_test_key()
_stripe_ready = False


def stripe_active() -> bool:
    return settings.features.stripe and settings.env.stripe_secret_key.startswith("sk_test_")


def _stripe():
    """The stripe module, configured once: test key, short timeout, one retry (safe: idempotency keys)."""
    global _stripe_ready
    if not _stripe_ready:
        stripe.api_key = settings.env.stripe_secret_key
        stripe.max_network_retries = 1
        stripe.default_http_client = stripe.HTTPXClient(timeout=STRIPE_TIMEOUT_S, allow_sync_methods=True)
        _stripe_ready = True
    return stripe


def link_test_card(member_id: str, name: str) -> bool:
    """Create a Stripe customer with the test Visa attached and save it on the member. Never raises."""
    if not stripe_active():
        return False
    try:
        s = _stripe()
        customer = s.Customer.create(name=name, metadata={"member_id": member_id},
                                     idempotency_key=f"speedmart-customer-{member_id}")
        pm = s.PaymentMethod.attach(TEST_PAYMENT_METHOD, customer=customer.id,
                                    idempotency_key=f"speedmart-attach-{member_id}")
    except Exception as e:  # network, auth, anything: the mock provider covers this member
        eventlog.log("stripe_link_failed", member_id=member_id, error=f"{type(e).__name__}: {e}")
        return False
    conn = db.connect()
    try:
        conn.execute("UPDATE members SET stripe_customer_id = ?, stripe_pm_id = ?, card_label = ? WHERE id = ?",
                     (customer.id, pm.id, DEFAULT_CARD_LABEL, member_id))
        conn.commit()
    finally:
        conn.close()
    eventlog.log("stripe_card_linked", member_id=member_id, customer_id=customer.id, payment_method_id=pm.id)
    return True


def ensure_card(member: dict[str, Any]) -> dict[str, Any]:
    """Backfill: a member without a Stripe card (the seeded Demo Shopper, or a signup while Stripe was down)
    gets one now. Returns the member row as it is afterwards."""
    if stripe_active() and not (member.get("stripe_customer_id") and member.get("stripe_pm_id")):
        if link_test_card(member["id"], member["name"]):
            conn = db.connect()
            try:
                member = dict(conn.execute("SELECT * FROM members WHERE id = ?", (member["id"],)).fetchone())
            finally:
                conn.close()
    return member


def stripe_charge(member: dict[str, Any], session_id: str, instruction: dict[str, Any], amount_cents: int,
                  attempt: int) -> dict[str, Any] | None:
    """One off-session PaymentIntent. None means "not a card problem": the caller falls back to mock."""
    member = ensure_card(member)
    if not (member.get("stripe_customer_id") and member.get("stripe_pm_id")):
        return None
    s = _stripe()
    try:
        intent = s.PaymentIntent.create(
            amount=amount_cents,
            currency=settings.store.get("currency", "USD").lower(),
            customer=member["stripe_customer_id"],
            payment_method=member["stripe_pm_id"],
            payment_method_types=["card"],
            off_session=True,
            confirm=True,
            description=settings.store["name"],
            metadata={"session_id": session_id, "instruction_id": instruction["instruction_id"]},
            idempotency_key=f"speedmart-{session_id}-{attempt}",
        )
    except s.CardError as e:
        intent_id = getattr(getattr(getattr(e, "error", None), "payment_intent", None), "id", None)
        return {"provider": "stripe_test", "provider_ref": intent_id, "status": DECLINED, "auth_code": None,
                "message": getattr(e, "user_message", None) or str(e) or "The card was declined."}
    except Exception as e:
        eventlog.log("stripe_fallback", session_id=session_id, error=f"{type(e).__name__}: {e}")
        return None
    if intent.status == "succeeded":
        return {"provider": "stripe_test", "provider_ref": intent.id, "status": AUTHORIZED,
                "auth_code": intent.id[-6:].upper(), "message": None}
    return {"provider": "stripe_test", "provider_ref": intent.id, "status": DECLINED, "auth_code": None,
            "message": f"The payment was not completed (Stripe status: {intent.status})."}


# --- providers ---

def mock_charge() -> dict[str, Any]:
    return {"provider": "mock", "provider_ref": None, "status": AUTHORIZED,
            "auth_code": f"MOCK{secrets.randbelow(10000):04d}", "message": None}


def charge(member: dict[str, Any], session_id: str, instruction: dict[str, Any], amount_cents: int,
           attempt: int, force_decline: bool = False) -> dict[str, Any]:
    """Run one charge. Returns {provider, provider_ref, status, auth_code, message}. Never raises."""
    if force_decline:  # 9.9: declined without calling Stripe
        provider = "stripe_test" if stripe_active() else "mock"
        return {"provider": provider, "provider_ref": None, "status": DECLINED, "auth_code": None,
                "message": FORCED_DECLINE_MESSAGE}
    if stripe_active():
        try:
            result = stripe_charge(member, session_id, instruction, amount_cents, attempt)
        except Exception as e:  # belt and braces: the demo must continue
            eventlog.log("stripe_fallback", session_id=session_id, error=f"{type(e).__name__}: {e}")
            result = None
        if result is not None:
            return result
    return mock_charge()


# --- records ---

def points_for(amount_usd: float, status: str) -> int:
    """9.10: floor(total) on an authorized payment, when loyalty is on."""
    if not settings.features.loyalty or status != AUTHORIZED:
        return 0
    return int(math.floor(amount_usd + 1e-9))


def payment_view(row: dict[str, Any], card_label: str | None) -> dict[str, Any]:
    """Payment (8.7) from a payments row."""
    instruction = json.loads(row["instruction_json"])
    amount_usd = cart_mod.to_usd(row["amount_cents"])
    return {
        "payment_id": row["id"],
        "instruction_id": instruction["instruction_id"],
        "provider": row["provider"],
        "provider_ref": row["provider_ref"],
        "amount_usd": amount_usd,
        "currency": row["currency"],
        "status": row["status"],
        "auth_code": row["auth_code"],
        "card_label": card_label or DEFAULT_CARD_LABEL,
        "points_earned": points_for(amount_usd, row["status"]),
        "created_at": row["created_at"],
    }


def attempts(session_id: str) -> int:
    conn = db.connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM payments WHERE store_session_id = ?", (session_id,)).fetchone()[0]
    finally:
        conn.close()


def record(session_id: str, member: dict[str, Any], amount_cents: int, result: dict[str, Any],
           instruction: dict[str, Any]) -> dict[str, Any]:
    """Save one payment attempt to SQLite and data/payments.log.jsonl; add loyalty points when authorized."""
    row = {
        "id": db.new_id("pay"),
        "store_session_id": session_id,
        "member_id": member["id"],
        "amount_cents": amount_cents,
        "currency": settings.store.get("currency", "USD"),
        "provider": result["provider"],
        "provider_ref": result["provider_ref"],
        "status": result["status"],
        "auth_code": result["auth_code"],
        "instruction_json": json.dumps(instruction),
        "created_at": db.now_iso(),
    }
    payment = payment_view(row, member.get("card_label"))
    conn = db.connect()
    try:
        conn.execute(f"INSERT INTO payments ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
        if payment["points_earned"]:
            conn.execute("UPDATE members SET points = points + ? WHERE id = ?", (payment["points_earned"], member["id"]))
        conn.commit()
    finally:
        conn.close()

    entry = {**payment, "session_id": session_id, "member_id": member["id"], "message": result.get("message"),
             "instruction": instruction}
    path = payments_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock, path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    eventlog.log("payment", session_id=session_id, member_id=member["id"], payment_id=payment["payment_id"],
                 provider=payment["provider"], status=payment["status"], amount_usd=payment["amount_usd"],
                 auth_code=payment["auth_code"], message=result.get("message"))
    return payment


def session_payments(session_id: str) -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM payments WHERE store_session_id = ? ORDER BY created_at, rowid",
                            (session_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

