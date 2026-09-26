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
DEFAULT_CARD_LABEL = db.DEMO_CARD_LABEL
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

def token_scope(member: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """The agent token's scope (8.6) for this member. Also what the permissions card (8.10) shows."""
    now = now or datetime.now(timezone.utc)
    return {
        "merchant": settings.store["name"],
        "max_amount_usd": float(member["budget_usd"]),
        "currency": settings.store.get("currency", "USD"),
        "single_use": True,
        "expires_at": _iso(now + INSTRUCTION_TTL),
    }


def pending_instruction(session_id: str) -> dict[str, Any] | None:
    """The instruction issued at quote time for this checkout, if any (a copy)."""
    with _lock:
        instr = _pending.get(session_id)
        return json.loads(json.dumps(instr)) if instr else None


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
            "scope": token_scope(member, now),
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

    _append_log({"type": "CHARGE", **payment, "session_id": session_id, "member_id": member["id"],
                 "message": result.get("message"), "instruction": instruction})
    eventlog.log("payment", session_id=session_id, member_id=member["id"], payment_id=payment["payment_id"],
                 provider=payment["provider"], status=payment["status"], amount_usd=payment["amount_usd"],
                 auth_code=payment["auth_code"], message=result.get("message"))
    return payment


def _append_log(entry: dict[str, Any]) -> None:
    path = payments_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock, path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


# --- refunds (Continue stage, 8.11) ---

REFUND_SUCCEEDED, REFUND_FAILED = "SUCCEEDED", "FAILED"


def mock_refund() -> dict[str, Any]:
    return {"provider": "mock", "provider_ref": f"re_mock_{secrets.token_hex(6)}", "status": REFUND_SUCCEEDED,
            "message": None}


def stripe_refund(payment: dict[str, Any], amount_cents: int, return_session_id: str,
                  attempt: int = 1, reason: str = "return") -> dict[str, Any] | None:
    """A partial Refund on the original PaymentIntent. None means "not a card problem": fall back to mock.
    A dispute refund (8.13) passes its dispute id as return_session_id: it keys the idempotency key."""
    s = _stripe()
    ref_key = "return_session_id" if reason == "return" else "dispute_id"
    try:
        refund = s.Refund.create(
            payment_intent=payment["provider_ref"],
            amount=amount_cents,
            metadata={"session_id": payment["store_session_id"], ref_key: return_session_id,
                      "payment_id": payment["id"], "reason": reason},
            idempotency_key=f"speedmart-refund-{return_session_id}-{attempt}",
        )
    except s.CardError as e:
        return {"provider": "stripe_test", "provider_ref": None, "status": REFUND_FAILED,
                "message": getattr(e, "user_message", None) or str(e) or "The refund was refused."}
    except Exception as e:
        eventlog.log("stripe_refund_fallback", return_session_id=return_session_id, error=f"{type(e).__name__}: {e}")
        return None
    if refund.status in ("succeeded", "pending"):  # test cards settle at once; pending still reaches the card
        return {"provider": "stripe_test", "provider_ref": refund.id, "status": REFUND_SUCCEEDED, "message": None}
    return {"provider": "stripe_test", "provider_ref": refund.id, "status": REFUND_FAILED,
            "message": f"The refund did not complete (Stripe status: {refund.status})."}


def refund(payment: dict[str, Any], amount_cents: int, return_session_id: str, attempt: int = 1,
           reason: str = "return") -> dict[str, Any]:
    """Refund part of an AUTHORIZED payment. Stripe test mode when the payment went through Stripe and Stripe is
    on; mock otherwise, or on any Stripe failure that is not a card error. Never raises. reason "return" (a
    RETURNING session) or "dispute" (8.13, return_session_id is then the dispute id)."""
    if payment["provider"] == "stripe_test" and payment.get("provider_ref") and stripe_active():
        try:
            result = stripe_refund(payment, amount_cents, return_session_id, attempt, reason)
        except Exception as e:  # belt and braces, like charge()
            eventlog.log("stripe_refund_fallback", return_session_id=return_session_id,
                         error=f"{type(e).__name__}: {e}")
            result = None
        if result is not None:
            return result
    return mock_refund()


def points_to_remove(amount_usd: float) -> int:
    """Loyalty on a refund: floor of the refunded amount (the mirror of points_for)."""
    return int(math.floor(amount_usd + 1e-9)) if settings.features.loyalty else 0


def refund_view(row: dict[str, Any], card_label: str | None) -> dict[str, Any]:
    """Refund (8.11) from a refunds row."""
    items = json.loads(row["items_json"])
    reason = row.get("reason") or "return"
    return {
        "refund_id": row["id"],
        "payment_id": row["payment_id"],
        "session_id": row["store_session_id"],
        # a dispute refund has no RETURNING session: the row stores the paid visit there (NOT NULL), shown as null
        "return_session_id": row["return_session_id"] if reason == "return" else None,
        "reason": reason,
        "dispute_id": row.get("dispute_id"),
        "provider": row["provider"],
        "provider_ref": row["provider_ref"],
        "amount_usd": cart_mod.to_usd(row["amount_cents"]),
        "currency": row["currency"],
        "status": row["status"],
        "items": items,
        "items_text": ", ".join(f"{i['qty']} {i['name']}" for i in items),
        "points_removed": row["points_removed"],
        "card_label": card_label or DEFAULT_CARD_LABEL,
        "created_at": row["created_at"],
    }


def record_refund(payment: dict[str, Any], return_session_id: str | None, member: dict[str, Any], amount_cents: int,
                  items: list[dict[str, Any]], result: dict[str, Any], reason: str = "return",
                  dispute_id: str | None = None) -> dict[str, Any]:
    """Save one refund to SQLite and data/payments.log.jsonl (type REFUND); take the points back.
    A dispute refund (reason "dispute", 8.13) has no return session: the paid visit fills that column."""
    succeeded = result["status"] == REFUND_SUCCEEDED
    points = points_to_remove(cart_mod.to_usd(amount_cents)) if succeeded else 0
    row = {
        "id": db.new_id("ref"),
        "payment_id": payment["id"],
        "store_session_id": payment["store_session_id"],
        "return_session_id": return_session_id or payment["store_session_id"],
        "member_id": member["id"],
        "amount_cents": amount_cents,
        "currency": payment["currency"],
        "provider": result["provider"],
        "provider_ref": result["provider_ref"],
        "status": result["status"],
        "items_json": json.dumps(items),
        "points_removed": 0,
        "created_at": db.now_iso(),
        "reason": reason,
        "dispute_id": dispute_id,
    }
    conn = db.connect()
    try:
        if points:
            have = conn.execute("SELECT points FROM members WHERE id = ?", (member["id"],)).fetchone()[0]
            row["points_removed"] = min(points, int(have or 0))  # never below zero
            conn.execute("UPDATE members SET points = points - ? WHERE id = ?", (row["points_removed"], member["id"]))
        conn.execute(f"INSERT INTO refunds ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
        conn.commit()
    finally:
        conn.close()
    view = refund_view(row, member.get("card_label"))
    _append_log({"type": "REFUND", **view, "member_id": member["id"], "message": result.get("message")})
    eventlog.log("refund", session_id=payment["store_session_id"], return_session_id=view["return_session_id"],
                 reason=reason, dispute_id=dispute_id, member_id=member["id"], refund_id=view["refund_id"], payment_id=payment["id"],
                 provider=view["provider"], provider_ref=view["provider_ref"], status=view["status"],
                 amount_usd=view["amount_usd"], items={i["sku"]: i["qty"] for i in items},
                 points_removed=view["points_removed"], message=result.get("message"))
    return view


def payment_refunds(payment_id: str) -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM refunds WHERE payment_id = ? ORDER BY created_at, rowid",
                            (payment_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def session_payments(session_id: str) -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM payments WHERE store_session_id = ? ORDER BY created_at, rowid",
                            (session_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

