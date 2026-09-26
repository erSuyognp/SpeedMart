"""Public API routes (catalog, session, gates). Filled in by S1.3 and S3.4."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend import eventlog, store
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
    """Quote: freezes the cart, IN_STORE -> CHECKOUT_PENDING. The payment instruction arrives in S4.1."""
    require_dev_routes(request)
    member_id = require_member(request)
    session = store.current_session()
    if session is None or session["member_id"] != member_id:
        raise ApiError(409, "no_active_session", "You are not in the store.")
    snapshot = store.freeze_cart()
    eventlog.log("dev_checkout", member_id=member_id, session_id=session["id"])
    return {"cart": snapshot}
