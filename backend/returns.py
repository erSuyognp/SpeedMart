"""Continue stage: returns with camera-verified refunds (7.1 RETURNING, 8.1 returns routes, 8.11, 8.12).

From a paid receipt (within RETURN_WINDOW, Stripe or mock payment) the shopper confirms with Face ID and a
RETURNING session takes the store lock, baseline = the shelf right now. The refund is computed the same way the
cart is, never accumulated: a unit counts as returned when the camera sees its SKU's shelf count rise above the
baseline, capped at what this purchase still has unrefunded. Anything else that shows up is ignored and listed
as "not from this purchase". Confirm refunds exactly the detected items plus their tax.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from backend import cart as cart_mod
from backend import db, eventlog, payments, shelf_state, store, ws
from backend.routes_api import ApiError
from backend.settings import settings

RETURN_WINDOW = timedelta(minutes=30)
REFUNDABLE_PROVIDERS = ("stripe_test", "mock")
NOT_FROM_PURCHASE = "not from this purchase"

router = APIRouter()
_confirm_lock = threading.Lock()  # check + refund + record as one step: a double tap never refunds twice
_last_detected: dict[str, tuple] = {}  # return session id -> last detection logged


class StartBody(BaseModel):
    session_id: str


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --- what a paid visit can still give back ---

def paid_payment(session_id: str) -> dict[str, Any] | None:
    return next((p for p in payments.session_payments(session_id) if p["status"] == payments.AUTHORIZED), None)


def purchased(session: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """sku -> {"sku", "name", "qty", "unit_cents"} from the frozen cart that was charged."""
    items = (session.get("final_cart") or {}).get("items") or []
    return {i["sku"]: {"sku": i["sku"], "name": i["name"], "qty": int(i["qty"]),
                       "unit_cents": cart_mod.to_cents(i["unit_price_usd"])} for i in items if i["qty"] > 0}


def refunded(payment_id: str) -> tuple[dict[str, int], int]:
    """(qty already refunded per SKU, cents already refunded) over this payment's successful refunds."""
    qty: dict[str, int] = {}
    cents = 0
    for row in payments.payment_refunds(payment_id):
        if row["status"] != payments.REFUND_SUCCEEDED:
            continue
        cents += row["amount_cents"]
        for item in json.loads(row["items_json"]):
            qty[item["sku"]] = qty.get(item["sku"], 0) + int(item["qty"])
    return qty, cents


def remaining(session: dict[str, Any], payment: dict[str, Any]) -> dict[str, int]:
    """Units of each SKU bought in this visit and not refunded yet (only SKUs with some left)."""
    done, _ = refunded(payment["id"])
    left = {sku: p["qty"] - done.get(sku, 0) for sku, p in purchased(session).items()}
    return {sku: n for sku, n in left.items() if n > 0}


def eligibility(session: dict[str, Any] | None, now: datetime | None = None) -> dict[str, Any]:
    """Can this visit start a return? {"eligible", "reason", "message", "deadline"}; reason is an error code."""
    now = now or datetime.now(timezone.utc)
    out = {"eligible": False, "reason": None, "message": "", "deadline": None}
    payment = paid_payment(session["id"]) if session and not session.get("return_of") else None
    if session is None or payment is None or session["state"] not in (store.PAID, store.CLOSED):
        out.update(reason="not_returnable", message="Only a paid visit can be returned.")
        return out
    if payment["provider"] not in REFUNDABLE_PROVIDERS:
        out.update(reason="not_returnable", message="This payment can't be refunded here.")
        return out
    deadline = _parse(payment["created_at"]) + RETURN_WINDOW
    out["deadline"] = _iso(deadline)
    if now > deadline:
        out.update(reason="return_window_closed", message="Returns close 30 minutes after you pay.")
        return out
    if not remaining(session, payment):
        out.update(reason="nothing_to_return", message="Everything from this visit is already refunded.")
        return out
    out.update(eligible=True)
    return out


# --- detection and money (computed, never accumulated) ---

def detect(baseline: dict[str, int], shelf_counts: dict[str, int], left: dict[str, int]) -> tuple[dict[str, int], dict[str, int]]:
    """(returned, ignored) per SKU. rise = shelf now - baseline; returned = min(rise, left); ignored = the rest."""
    returned, ignored = {}, {}
    for sku in settings.skus:  # catalog order
        rise = max(0, shelf_counts.get(sku, 0) - baseline.get(sku, 0))
        take = min(rise, left.get(sku, 0))
        if take:
            returned[sku] = take
        if rise - take:
            ignored[sku] = rise - take
    return returned, ignored


def refund_amount(returned: dict[str, int], left: dict[str, int], unit_cents: dict[str, int],
                  paid_cents: int, refunded_cents: int, tax_rate: float) -> tuple[int, int, int]:
    """(subtotal, tax, total) in cents for the returned units, tax at the store rate with the cart's rounding.
    Returning everything that is left refunds exactly what is left of the payment, so rounding over several
    returns never gives back more (or less) than was charged. Never more than what is left of the payment."""
    subtotal = sum(unit_cents[sku] * n for sku, n in returned.items())
    tax = cart_mod.tax_cents(subtotal, tax_rate)
    total = subtotal + tax
    left_cents = max(0, paid_cents - refunded_cents)
    if returned and returned == left:
        total = left_cents
    total = min(total, left_cents)
    return subtotal, total - subtotal, total


def snapshot(session: dict[str, Any]) -> dict[str, Any]:
    """ReturnSnapshot (8.12) for a RETURNING session."""
    original = store.get_session(session["return_of"])
    payment = paid_payment(original["id"]) if original else None
    bought = purchased(original) if original else {}
    left = remaining(original, payment) if payment else {}
    shelf = shelf_state.shelf_counts() if shelf_state.has_snapshot() else dict(session["baseline"])
    returned, ignored = detect(session["baseline"], shelf, left)
    _, refunded_cents = refunded(payment["id"]) if payment else ({}, 0)
    subtotal, tax, total = refund_amount(returned, left, {s: b["unit_cents"] for s, b in bought.items()},
                                         payment["amount_cents"] if payment else 0, refunded_cents,
                                         settings.store["tax_rate"])
    items = [{"sku": sku, "name": bought[sku]["name"], "qty": n,
              "unit_price_usd": cart_mod.to_usd(bought[sku]["unit_cents"]),
              "line_total_usd": cart_mod.to_usd(bought[sku]["unit_cents"] * n)} for sku, n in returned.items()]
    return {
        "session_id": session["id"],
        "state": session["state"],
        "original_session_id": session["return_of"],
        "payment_id": payment["id"] if payment else None,
        "returnable": [{"sku": sku, "name": bought[sku]["name"], "qty": n} for sku, n in left.items()],
        "items": items,
        "ignored": [{"sku": sku, "name": settings.skus[sku].name, "qty": n, "message": NOT_FROM_PURCHASE}
                    for sku, n in ignored.items()],
        "subtotal_usd": cart_mod.to_usd(subtotal),
        "tax_usd": cart_mod.to_usd(tax),
        "total_usd": cart_mod.to_usd(total),
        "expires_at": _iso(_parse(session["started_at"]) + timedelta(minutes=store.RETURN_TIMEOUT_MINUTES)),
        "updated_at": db.now_iso(),
    }


def _publish(session: dict[str, Any], snap: dict[str, Any]) -> None:
    ws.manager.publish({"type": "return", "data": snap}, member_id=session["member_id"], admin=True)


def on_shelf_change(session: dict[str, Any]) -> dict[str, Any]:
    """store.on_shelf_change hands RETURNING sessions here: recompute, log changes, push to the phone."""
    snap = snapshot(session)
    key = (tuple((i["sku"], i["qty"]) for i in snap["items"]), tuple((i["sku"], i["qty"]) for i in snap["ignored"]))
    if _last_detected.get(session["id"]) != key:
        _last_detected[session["id"]] = key
        eventlog.log("return_detected", session_id=session["id"], return_of=session["return_of"],
                     items={i["sku"]: i["qty"] for i in snap["items"]},
                     ignored={i["sku"]: i["qty"] for i in snap["ignored"]}, total_usd=snap["total_usd"])
    _publish(session, snap)
    return snap


def own_return(member_id: str) -> dict[str, Any] | None:
    session = store.current_session()
    if session is None or session["member_id"] != member_id or session["state"] != store.RETURNING:
        return None
    return session


def _expired(session: dict[str, Any]) -> bool:
    age = datetime.now(timezone.utc) - _parse(session["started_at"])
    return age > timedelta(minutes=store.RETURN_TIMEOUT_MINUTES)


# --- routes (8.1) ---

@router.post("/api/returns/start")
def start(body: StartBody, request: Request):
    """Face ID (fresh verification, 7.2) starts a RETURNING session for one of your paid visits.
    401 not signed in / not verified, 404 not yours, 409 not returnable / window closed / nothing left /
    store occupied, 503 camera offline. Refusals that need no Face ID come first so they do not use it up."""
    from backend import auth_passkeys, members  # local: they import routes_api

    member = members.current_member(request)
    original = store.get_session(body.session_id)
    if original is None or original["member_id"] != member["id"]:
        raise ApiError(404, "not_found", "No visit to return here.")
    mine = own_return(member["id"])
    if mine is not None and mine["return_of"] == original["id"]:
        return {"return": snapshot(mine)}  # tapped twice: already returning, nothing to verify again
    active = store.current_session()
    if active is not None:
        occupant = members.get_member(active["member_id"])
        eventlog.log("store_occupied", member_id=member["id"], occupant_session_id=active["id"])
        raise store.StoreOccupied(members.first_name(occupant["name"]) if occupant else "Someone")
    check = eligibility(original)
    if not check["eligible"]:
        eventlog.log("return_refused", member_id=member["id"], session_id=original["id"], reason=check["reason"])
        raise ApiError(409, check["reason"], check["message"])
    if not shelf_state.has_snapshot() or shelf_state.last_snapshot_age_ms() > store.VISION_MAX_AGE_MS:
        raise store.VisionUnavailable()
    auth_passkeys.consume_fresh_verification(request, member["id"])
    session = store.start_return(member["id"], original["id"])  # re-checks the lock under store._lock
    snap = on_shelf_change(session)
    eventlog.log("return_started", member_id=member["id"], session_id=session["id"], return_of=original["id"],
                 returnable={i["sku"]: i["qty"] for i in snap["returnable"]})
    return {"return": snap}


@router.get("/api/returns/current")
def current(request: Request):
    from backend import members

    member = members.current_member(request)
    session = own_return(member["id"])
    return {"return": snapshot(session) if session else None}


def card_last4(card_label: str | None) -> str | None:
    found = re.findall(r"\d{4}", card_label or "")
    return found[-1] if found else None


@router.post("/api/returns/confirm")
def confirm(request: Request):
    """Refund exactly the items the camera saw come back, plus their tax. RETURNING -> CLOSED on success."""
    from backend import agent, members

    member = members.current_member(request)
    with _confirm_lock:
        session = own_return(member["id"])
        if session is None:
            raise ApiError(409, "no_active_return", "There is no return in progress.")
        if _expired(session):
            store.cancel(session["id"], reason="timeout")
            raise ApiError(409, "return_expired", "This return timed out. Start again from your receipt.")
        snap = snapshot(session)
        if not snap["items"]:
            raise ApiError(409, "nothing_returned", "Place the item back on its bay first.")
        payment = paid_payment(session["return_of"])
        amount_cents = cart_mod.to_cents(snap["total_usd"])
        items = [{"sku": i["sku"], "name": i["name"], "qty": i["qty"]} for i in snap["items"]]
        attempt = sum(1 for r in payments.payment_refunds(payment["id"]) if r["return_session_id"] == session["id"]) + 1
        result = payments.refund(payment, amount_cents, session["id"], attempt)
        refund = payments.record_refund(payment, session["id"], member, amount_cents, items, result)
        if refund["status"] != payments.REFUND_SUCCEEDED:
            return {"refund": refund, "message": result.get("message") or "The refund did not go through."}
        store.end_return(session["id"], reason="refunded")
    _last_detected.pop(session["id"], None)
    line = agent.refund_line(refund["amount_usd"], card_last4(refund["card_label"]))
    eventlog.log("agent_line", session_id=session["id"], source="template", decision={"kind": "refund"}, line=line)
    ws.manager.publish({"type": "agent", "data": {"line": line}}, member_id=member["id"], admin=True)
    ws.broadcast_gate(member["id"], "refunded")
    return {"refund": refund, "agent_line": line}


@router.post("/api/returns/cancel")
def cancel(request: Request):
    """Changed your mind: RETURNING -> CLOSED, no refund."""
    from backend import members

    member = members.current_member(request)
    session = own_return(member["id"])
    if session is not None:
        store.end_return(session["id"], reason="return_cancelled")
        _last_detected.pop(session["id"], None)
        eventlog.log("return_cancelled", member_id=member["id"], session_id=session["id"])
    return {"ok": True}
