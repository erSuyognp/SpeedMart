"""Public API routes (catalog, session, gates, checkout). S1.3, S3.4, S4.1."""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend import eventlog, serial_bridge, shelf_state, store
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
GATE_OPEN_S = 3  # 9.7: gate LED green this long after a successful entry


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
    serial_bridge.send_timed("GATE,OPEN", "GATE,IDLE", GATE_OPEN_S)
    eventlog.log("gate_enter", member_id=member["id"], session_id=session["id"])
    return {"session": public_session(session), "cart": store.cart_for(session)}


def quote_for(member_id: str) -> dict[str, Any]:
    """Freeze the cart for checkout (IN_STORE -> CHECKOUT_PENDING). Re-quoting a pending checkout returns it
    again. An empty cart closes the session instead: nothing to pay."""
    session = own_active_session(member_id)
    if session["state"] == store.IN_STORE:
        live = store.compute_live_cart(session)
        if not live["items"]:
            closed = store.close(session["id"], reason="empty_cart")
            eventlog.log("exit_empty", member_id=member_id, session_id=session["id"])
            return {"cart": store.cart_for(closed), "instruction": None}
        cart = store.freeze_cart()
    else:
        cart = store.cart_for(session)
    return {"cart": cart, "instruction": None}


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
        eventlog.log("gate_exit_cancel", member_id=member["id"], session_id=session["id"])
    return {"ok": True}
