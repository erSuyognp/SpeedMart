"""Public API routes (catalog, session, gates, checkout). S1.3, S3.4, S4.1."""

from __future__ import annotations

import secrets
import threading
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend import eventlog, payments, shelf_state, store, ws
from backend.settings import settings

# HTTP status for each StoreError code. Unlisted codes are 400.
STORE_ERROR_STATUS = {
    "store_occupied": 409,
    "vision_unavailable": 503,
    "invalid_state": 409,
    "no_active_session": 409,
    "cart_not_empty": 409,
    "unknown_member": 404,
    "unknown_sku": 400,
}


class ApiError(Exception):
    """Raise from a route to answer {"error": code, "message": message} with `status`."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def error_response(status: int, code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, "message": message, **extra})


def store_error_response(e: store.StoreError) -> JSONResponse:
    extra = {"occupant_first_name": e.occupant_first_name} if isinstance(e, store.StoreOccupied) else {}
    return error_response(STORE_ERROR_STATUS.get(e.code, 400), e.code, e.message, **extra)


router = APIRouter()


def member_id_from(request: Request) -> str | None:
    """Member signed in on this browser (session cookie), if any."""
    return request.session.get("member_id")


def require_member(request: Request) -> str:
    member_id = member_id_from(request)
    if not member_id:
        raise ApiError(401, "not_logged_in", "Please sign in first.")
    return member_id


def public_session(session: dict[str, Any]) -> dict[str, Any]:
    return {"id": session["id"], "member_id": session["member_id"], "state": session["state"],
            "baseline": session["baseline"], "started_at": session["started_at"]}


@router.get("/api/store/current")
def store_current(request: Request):
    """The active session and its cart, only for the shopper in it (or an admin)."""
    session = store.current_session()
    if session is None or not (request.session.get("admin") or session["member_id"] == member_id_from(request)):
        return {"session": None}
    return {"session": public_session(session), "cart": store.cart_for(session)}


# --- gates-off fallback (F8 off): "Start shopping" and "Checkout" buttons instead of QR gates ---

def require_dev_routes(request: Request) -> None:
    """The fallback routes exist only while gates are off; admins keep them for testing either way."""
    if settings.features.gates and not request.session.get("admin"):
        raise ApiError(404, "not_available", "Use the entry and exit gates.")


@router.post("/api/dev/start")
def dev_start(request: Request):
    require_dev_routes(request)
    member_id = require_member(request)
    session = store.start_session(member_id)
    eventlog.log("dev_start", member_id=member_id, session_id=session["id"])
    return {"session": public_session(session), "cart": store.cart_for(session)}


@router.post("/api/dev/checkout")
def dev_checkout(request: Request):
    """Gates-off "Checkout" button: the same quote as the exit gate, without a gate token."""
    require_dev_routes(request)
    member_id = require_member(request)
    result = quote_for(member_id)
    eventlog.log("dev_checkout", member_id=member_id, session_id=result["cart"]["session_id"])
    return result


# --- entry + exit gates (F8, S3.4). Gate tokens come from the QR codes (13.2). ---

class GateBody(BaseModel):
    gate_token: str = ""


def require_gates() -> None:
    if not settings.features.gates:
        raise ApiError(404, "gates_off", "The gates are off for this demo. Use the buttons in your cart.")


def check_gate_token(given: str, expected: str, gate: str) -> None:
    if not expected or not secrets.compare_digest(given.encode(), expected.encode()):
        eventlog.log("gate_bad_token", gate=gate)
        raise ApiError(403, "bad_gate_token", "This code is not valid. Scan the code on the gate again.")


def own_active_session(member_id: str) -> dict[str, Any]:
    session = store.current_session()
    if session is None or session["member_id"] != member_id:
        raise ApiError(409, "no_active_session", "You are not in the store.")
    return session


@router.post("/api/gate/enter")
def gate_enter(body: GateBody, request: Request):
    """Face ID (fresh verification) at the entry gate starts a session. 403 token, 401 not verified, 409 occupied."""
    from backend import auth_passkeys, members  # local: they import this module

    require_gates()
    check_gate_token(body.gate_token, settings.env.entry_gate_token, "entry")
    member = members.current_member(request)
    session = store.current_session()
    if session is not None and session["member_id"] == member["id"]:
        # Scanned twice / tapped twice: already inside, nothing to verify or consume.
        return {"session": public_session(session), "cart": store.cart_for(session)}
    # Refusals that do not need Face ID come first, so they do not use up a fresh verification.
    if session is not None:
        occupant = members.get_member(session["member_id"])
        raise store.StoreOccupied(members.first_name(occupant["name"]) if occupant else "Someone")
    if not shelf_state.has_snapshot() or shelf_state.last_snapshot_age_ms() > store.VISION_MAX_AGE_MS:
        raise store.VisionUnavailable()
    auth_passkeys.consume_fresh_verification(request, member["id"])
    session = store.start_session(member["id"])  # re-checks the lock under store._lock
    eventlog.log("gate_enter", member_id=member["id"], session_id=session["id"])
    return {"session": public_session(session), "cart": store.cart_for(session)}


def quote_for(member_id: str) -> dict[str, Any]:
    """Freeze the cart for checkout (IN_STORE -> CHECKOUT_PENDING) and issue the instruction (8.6).
    Re-quoting a pending checkout returns it again. An empty cart closes the session instead: nothing to pay."""
    from backend import intent, members

    session = own_active_session(member_id)
    if session["state"] == store.IN_STORE:
        live = store.compute_live_cart(session)
        if not live["items"]:
            closed = store.close(session["id"], reason="empty_cart")
            eventlog.log("exit_empty", member_id=member_id, session_id=session["id"])
            return {"cart": store.cart_for(closed), "instruction": None, "plan_check": None}
        cart = store.freeze_cart()
    else:
        cart = store.cart_for(session)
    member = payments.ensure_card(members.get_member(member_id))  # Stripe backfill (e.g. the demo member)
    # F18: when the shopper made a plan this visit, the exit screen shows how the cart compares (null otherwise).
    plan = intent.current_plan(member_id)
    plan_check = intent.compare_cart_to_plan(cart, plan) if plan else None
    return {"cart": cart, "instruction": payments.instruction_for(session["id"], cart, member),
            "plan_check": plan_check}


@router.post("/api/gate/exit/quote")
def gate_exit_quote(body: GateBody, request: Request):
    from backend import members

    require_gates()
    check_gate_token(body.gate_token, settings.env.exit_gate_token, "exit")
    member = members.current_member(request)
    result = quote_for(member["id"])
    eventlog.log("gate_exit_quote", member_id=member["id"], session_id=result["cart"]["session_id"],
                 total_usd=result["cart"]["total_usd"])
    return result


@router.post("/api/gate/exit/cancel")
def gate_exit_cancel(request: Request):
    """"Keep shopping": CHECKOUT_PENDING -> IN_STORE, cart live again. Also the gates-off fallback's cancel."""
    from backend import members

    member = members.current_member(request)
    session = own_active_session(member["id"])
    if session["state"] == store.CHECKOUT_PENDING:
        store.unfreeze_cart()
        payments.forget_instruction(session["id"])
        eventlog.log("gate_exit_cancel", member_id=member["id"], session_id=session["id"])
    return {"ok": True}


# --- approve, receipt (S4.1) ---

_approve_lock = threading.Lock()  # check + charge + record as one step: a double tap never charges twice


@router.post("/api/gate/exit/approve")
def gate_exit_approve(request: Request):
    """Charge the frozen cart. Needs a fresh Face ID when passkeys are on (7.2). Works with gates off too."""
    from backend import admin, auth_passkeys, members

    member = members.current_member(request)
    session = own_active_session(member["id"])
    if session["state"] != store.CHECKOUT_PENDING:
        raise ApiError(409, "invalid_state", "Scan the exit code first to review your cart.")
    cart = store.cart_for(session)
    instruction = payments.instruction_for(session["id"], cart, member)
    if payments.over_scope(cart, instruction):
        limit = instruction["agent_token"]["scope"]["max_amount_usd"]
        eventlog.log("approve_over_scope", member_id=member["id"], session_id=session["id"],
                     total_usd=cart["total_usd"], max_amount_usd=limit)
        raise ApiError(409, "over_scope", f"This is over your ${limit:g} limit. Put something back to continue.")
    instruction["cardholder_confirmation"] = auth_passkeys.consume_fresh_verification(request, member["id"])

    with _approve_lock:
        session = store.get_session(session["id"])
        if session is None or session["state"] != store.CHECKOUT_PENDING:
            raise ApiError(409, "invalid_state", "This checkout is already finished.")
        amount_cents = payments.cart_mod.to_cents(cart["total_usd"])
        attempt = payments.attempts(session["id"]) + 1
        result = payments.charge(member, session["id"], instruction, amount_cents, attempt,
                                 force_decline=admin.force_decline)
        payment = payments.record(session["id"], member, amount_cents, result, instruction)
        if payment["status"] == payments.AUTHORIZED:
            store.mark_paid(session["id"])
            payments.forget_instruction(session["id"])

    if payment["status"] == payments.AUTHORIZED:
        return {"payment": payment}
    ws.broadcast_gate(member["id"], "declined")
    return {"payment": payment, "message": result.get("message") or "The card was declined."}


@router.get("/api/receipt/{session_id}")
def receipt(session_id: str, request: Request):
    """Paid receipt with items, payment and points (11.5). Only the shopper (or an admin) may see it.
    Viewing a PAID receipt closes the session (7.1)."""
    from backend import members

    session = store.get_session(session_id)
    viewer = request.session.get("member_id")
    if session is None or not (session["member_id"] == viewer or request.session.get("admin")):
        raise ApiError(404, "not_found", "No receipt here.")
    member = members.get_member(session["member_id"]) or {}
    rows = payments.session_payments(session_id)
    paid = next((r for r in rows if r["status"] == payments.AUTHORIZED), None)
    shown = paid or (rows[-1] if rows else None)
    payment = payments.payment_view(shown, member.get("card_label")) if shown else None
    if session["state"] == store.PAID and session["member_id"] == viewer:
        session = store.close(session_id, reason="receipt_viewed")
    cart = session["final_cart"] or store.cart_for(session)
    return {
        "session_id": session_id,
        "state": session["state"],
        "store_name": settings.store["name"],
        "items": cart["items"],
        "subtotal_usd": cart["subtotal_usd"],
        "tax_usd": cart["tax_usd"],
        "total_usd": cart["total_usd"],
        "paid": paid is not None,
        "payment": payment,
        "loyalty": settings.features.loyalty,
        "points_earned": payment["points_earned"] if payment else 0,
        "points_total": int(member.get("points") or 0),
    }
