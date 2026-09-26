"""Cart disputes with camera evidence (8.13, F20 `disputes`).

"Not mine?" on a cart row (store.html), a tap on an item at the exit (exit.html) or "Report a problem" on the
receipt opens a dispute for one unit of a SKU. The store rechecks with the camera first:

    shown  = the cart the shopper sees (the frozen cart at the exit), or what is still unrefunded after paying
    camera = clamp(baseline - shelf_now, 0, baseline): what the latest stable shelf alone puts in the cart
    camera < shown  -> the unit is back on the shelf: correct it at once ("resolved_camera")
    otherwise       -> "needs_review" with the shelf photos of the item's home bay

The photos are crops of one bay each, never the full frame, saved by the vision worker under
data/evidence/<session_id>/ and announced on POST /internal/evidence. "Remove anyway" takes the unit out with a
signed override (source "dispute"); after paying it is a refund through the returns refund path (reason
"dispute"). At most MAX_REMOVE_ANYWAY of those per visit, then "Please ask a staff member". Evidence is deleted
once the visit is over (cancelled / empty exit at once, a paid visit when its 30 minute report window closes),
unless the visit has an open or resolved dispute: then it is kept EVIDENCE_HOLD after the last dispute.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend import admin, db, eventlog, payments, returns, shelf_state, store
from backend import cart as cart_mod
from backend.routes_api import ApiError
from backend.settings import settings

MAX_REMOVE_ANYWAY = 2  # "Remove anyway" disputes per visit (shopping and after paying together)
EVIDENCE_HOLD = timedelta(hours=24)  # evidence of a disputed visit is kept this long after its last dispute
REPORT_WINDOW = returns.RETURN_WINDOW  # "Report a problem" on the receipt: 30 minutes after paying, like returns
CLEANUP_SECONDS = 30
PRIVACY_LINE = "These photos show only the shelf and are deleted after your visit."
STAFF_LINE = "Please ask a staff member."

SHOPPING, CHECKOUT, AFTER_PURCHASE = "shopping", "checkout", "after_purchase"
RESOLVED_CAMERA, NEEDS_REVIEW, KEPT, REMOVED, REFUNDED = "resolved_camera", "needs_review", "kept", "removed", "refunded"
OPEN, RESOLVED, WITHDRAWN = "open", "resolved", "withdrawn"
REMOVE_ANYWAY = (REMOVED, REFUNDED)  # outcomes that count toward MAX_REMOVE_ANYWAY

SESSION_RE = re.compile(r"^ses_[A-Za-z0-9_-]{1,40}$")
FILE_RE = re.compile(r"^bay\d{1,3}_[A-Za-z0-9_-]{1,64}\.jpg$")

router = APIRouter()
admin_router = APIRouter(dependencies=[Depends(admin.require_admin)])
_lock = threading.Lock()  # recheck + change + record as one step: a double tap never removes or refunds twice


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def require_disputes() -> None:
    if not settings.features.disputes:
        raise ApiError(404, "disputes_off", "Disputes are off for this demo. Please ask a staff member.")


def evidence_root() -> Path:
    return db.DATA_DIR / "evidence"


# --- evidence notices from the vision worker (POST /internal/evidence) ---

class EvidenceNotice(BaseModel):
    session_id: str
    bay: int
    kind: Literal["baseline", "change"]
    file: str
    ts: int  # ms since the epoch, when the crop was taken
    width: int
    height: int
    units: list[int] = []  # unit tag ids in the bay at that moment
    tags: dict[str, list[float]] = {}  # tag id -> [x, y, w, h] in crop pixels


def _check_internal_token(request: Request) -> None:
    token = request.headers.get("X-Internal-Token", "")
    expected = settings.env.internal_token
    if not expected or not secrets.compare_digest(token.encode(), expected.encode()):
        raise ApiError(401, "bad_internal_token", "Missing or wrong X-Internal-Token.")


@router.post("/internal/evidence")
def evidence_notice(body: EvidenceNotice, request: Request):
    """The worker saved one bay crop for the shopping session: remember it so the dispute flow can show it.
    409 when the session is no longer shopping (the worker then deletes the file)."""
    _check_internal_token(request)
    require_disputes()
    if not SESSION_RE.match(body.session_id) or not FILE_RE.match(body.file):
        raise ApiError(422, "bad_evidence", "Unexpected session id or file name.")
    if body.bay not in {b.id for b in settings.bays}:
        raise ApiError(422, "bad_evidence", f"Unknown bay {body.bay}.")
    session = store.get_session(body.session_id)
    if session is None or session["state"] not in store.SHOPPING_STATES:
        raise ApiError(409, "session_not_active", "That session is not shopping any more.")
    if not (evidence_root() / body.session_id / body.file).is_file():
        raise ApiError(404, "file_missing", "The crop is not on disk.")
    tags = {str(int(k)): [round(float(v), 1) for v in box[:4]] for k, box in body.tags.items()
            if k.lstrip("-").isdigit() and len(box) >= 4}
    captured = datetime.fromtimestamp(body.ts / 1000, tz=timezone.utc)
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO evidence (store_session_id, bay, kind, file, captured_at, width, height, units_json, "
            "tags_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (body.session_id, body.bay, body.kind, body.file,
             captured.isoformat(timespec="milliseconds").replace("+00:00", "Z"), body.width, body.height,
             json.dumps(sorted(set(body.units))), json.dumps(tags), db.now_iso()))
        conn.commit()
    finally:
        conn.close()
    eventlog.log("evidence_saved", session_id=body.session_id, bay=body.bay, kind=body.kind, file=body.file,
                 units=sorted(set(body.units)))
    return {"ok": True}


def crops(session_id: str, bay: int) -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM evidence WHERE store_session_id = ? AND bay = ? ORDER BY captured_at, id",
                            (session_id, bay)).fetchall()
        return [{**dict(r), "units": json.loads(r["units_json"]), "tags": json.loads(r["tags_json"])} for r in rows]
    finally:
        conn.close()


def home_bay(sku: str) -> int:
    bay = next((b.id for b in settings.bays if b.sku == sku), None)
    if bay is None:
        bay = next(u.home_bay for u in settings.units.values() if u.sku == sku)
    return bay


def build_evidence(session_id: str, sku: str) -> dict[str, Any]:
    """The baseline crop and the latest crop of the SKU's home bay, and the missing unit's last known outline
    (fractions of the crop, from the newest crop that still saw its tag). Photos are None when none were saved."""
    bay = home_bay(sku)
    rows = crops(session_id, bay)
    out: dict[str, Any] = {"bay": bay, "card": bay + 1, "tag_id": None, "before": None, "now": None, "outline": None}
    if not rows:
        return out
    before = next((r for r in rows if r["kind"] == "baseline"), rows[0])
    now = rows[-1]
    sku_units = {t for t, u in settings.units.items() if u.sku == sku}
    missing = sorted((set(before["units"]) - set(now["units"])) & sku_units) \
        or sorted({int(t) for r in rows for t in r["tags"]} & sku_units - set(now["units"]))
    if missing:
        out["tag_id"] = missing[0]
        seen = next((r for r in reversed(rows) if str(missing[0]) in r["tags"]), None)
        if seen is not None and seen["width"] > 0 and seen["height"] > 0:
            x, y, w, h = seen["tags"][str(missing[0])]
            out["outline"] = {"x": round(x / seen["width"], 4), "y": round(y / seen["height"], 4),
                              "w": round(w / seen["width"], 4), "h": round(h / seen["height"], 4)}
    for key, row in (("before", before), ("now", now)):
        out[key] = {"file": row["file"], "path": f"data/evidence/{session_id}/{row['file']}",
                    "captured_at": row["captured_at"]}
    return out


# --- dispute records ---

def _row(dispute_id: str) -> dict[str, Any] | None:
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM disputes WHERE id = ?", (dispute_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def remove_anyway_used(session_id: str) -> int:
    conn = db.connect()
    try:
        return conn.execute(
            f"SELECT COUNT(*) FROM disputes WHERE store_session_id = ? AND outcome IN ({','.join('?' * len(REMOVE_ANYWAY))})",
            (session_id, *REMOVE_ANYWAY)).fetchone()[0]
    finally:
        conn.close()


def _insert(session: dict[str, Any], member_id: str, sku: str, stage: str, outcome: str, status: str,
            evidence: dict[str, Any], amount_cents: int | None = None) -> dict[str, Any]:
    row = {"id": db.new_id("dsp"), "store_session_id": session["id"], "member_id": member_id, "sku": sku,
           "stage": stage, "outcome": outcome, "status": status, "amount_cents": amount_cents, "refund_id": None,
           "evidence_json": json.dumps(evidence), "created_at": db.now_iso(),
           "resolved_at": db.now_iso() if status != OPEN else None}
    conn = db.connect()
    try:
        conn.execute(f"INSERT INTO disputes ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
        conn.commit()
    finally:
        conn.close()
    eventlog.log("dispute_opened", dispute_id=row["id"], session_id=session["id"], member_id=member_id, sku=sku,
                 stage=stage, outcome=outcome, amount_usd=cart_mod.to_usd(amount_cents) if amount_cents else None,
                 evidence=[p["path"] for p in (evidence.get("before"), evidence.get("now")) if p])
    return row


def _settle(dispute_id: str, outcome: str, status: str, amount_cents: int | None = None,
            refund_id: str | None = None) -> dict[str, Any]:
    conn = db.connect()
    try:
        conn.execute("UPDATE disputes SET outcome = ?, status = ?, amount_cents = COALESCE(?, amount_cents), "
                     "refund_id = COALESCE(?, refund_id), resolved_at = ? WHERE id = ?",
                     (outcome, status, amount_cents, refund_id, db.now_iso(), dispute_id))
        conn.commit()
    finally:
        conn.close()
    return _row(dispute_id)


def _message(row: dict[str, Any], refund: dict[str, Any] | None = None) -> str:
    amount = f"${(row['amount_cents'] or 0) / 100:.2f}"
    after = row["stage"] == AFTER_PURCHASE
    if row["outcome"] == RESOLVED_CAMERA:
        if after:
            from backend import agent  # local: agent is heavy and only needed here

            return "Our mistake. " + agent.refund_line(cart_mod.to_usd(row["amount_cents"] or 0),
                                                      returns.card_last4((refund or {}).get("card_label")))
        return "Our mistake, removed from your cart."
    if row["outcome"] == NEEDS_REVIEW:
        return "The shelf camera still sees it gone. Compare the photos."
    if row["outcome"] == KEPT:
        return "Thanks. Nothing was refunded." if after else "Thanks. It stays in your cart."
    if row["outcome"] == REMOVED:
        return "Removed. Our team will review the shelf photos."
    if row["outcome"] == REFUNDED:
        return f"Refunded {amount}. Our team will review the shelf photos."
    return ""


def view(row: dict[str, Any], url_prefix: str = "/api/disputes/evidence",
         refund: dict[str, Any] | None = None) -> dict[str, Any]:
    """Dispute (8.13). Photo urls point at the owner's route (or the admin route for the admin page)."""
    ev = json.loads(row["evidence_json"] or "null")
    root = evidence_root() / row["store_session_id"]

    def photo(p: dict[str, Any] | None) -> dict[str, Any] | None:
        if not p or not (root / p["file"]).is_file():  # deleted after the visit: no photo
            return None
        return {"url": f"{url_prefix}/{row['store_session_id']}/{p['file']}", "captured_at": p["captured_at"],
                "outline": ev.get("outline")}

    return {
        "dispute_id": row["id"],
        "session_id": row["store_session_id"],
        "sku": row["sku"],
        "name": settings.skus[row["sku"]].name if row["sku"] in settings.skus else row["sku"],
        "stage": row["stage"],
        "outcome": row["outcome"],
        "status": row["status"],
        "message": _message(row, refund),
        "evidence": None if ev is None else {
            "bay": ev["bay"], "card": ev["card"], "tag_id": ev["tag_id"],
            "before": photo(ev.get("before")), "now": photo(ev.get("now")), "privacy": PRIVACY_LINE},
        "remove_anyway_left": max(0, MAX_REMOVE_ANYWAY - remove_anyway_used(row["store_session_id"])),
        "amount_usd": cart_mod.to_usd(row["amount_cents"]) if row["amount_cents"] is not None else None,
        "refund_id": row["refund_id"],
        "created_at": row["created_at"],
        "resolved_at": row["resolved_at"],
    }


# --- recheck helpers ---

def _qty(cart: dict[str, Any], sku: str) -> int:
    return next((int(i["qty"]) for i in cart["items"] if i["sku"] == sku), 0)


def _vision_fresh() -> bool:
    return shelf_state.has_snapshot() and 0 <= shelf_state.last_snapshot_age_ms() <= store.VISION_MAX_AGE_MS


def _own_shopping(member_id: str) -> dict[str, Any] | None:
    session = store.current_session()
    if session is None or session["member_id"] != member_id or session["state"] not in store.SHOPPING_STATES:
        return None
    return session


def _remove_from_cart(session: dict[str, Any], sku: str) -> tuple[dict[str, Any], int]:
    """One unit out of the live or frozen cart. (cart after, cents it took off the total)."""
    before = cart_mod.to_cents(store.cart_for(session)["total_usd"])
    cart = store.remove_one(sku, source="dispute")
    payments.forget_instruction(session["id"])  # the exit quote issues a new one for the new total
    return cart, max(0, before - cart_mod.to_cents(cart["total_usd"]))


def _paid_visit(member_id: str, session_id: str, sku: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """A paid visit of this member that can still be reported: (session, payment), else ApiError."""
    session = store.get_session(session_id)
    if session is None or session["member_id"] != member_id:
        raise ApiError(404, "not_found", "No visit to report here.")
    payment = returns.paid_payment(session_id) if not session.get("return_of") else None
    if payment is None or session["state"] not in (store.PAID, store.CLOSED) \
            or payment["provider"] not in returns.REFUNDABLE_PROVIDERS:
        raise ApiError(409, "not_disputable", "Only a paid visit can be reported.")
    if datetime.now(timezone.utc) > _parse(payment["created_at"]) + REPORT_WINDOW:
        raise ApiError(409, "dispute_window_closed", "Problems can be reported for 30 minutes after you pay. "
                       + STAFF_LINE)
    if returns.remaining(session, payment).get(sku, 0) < 1:
        raise ApiError(409, "nothing_to_refund", "That item is not on this receipt, or it is already refunded.")
    return session, payment


def _nobody_since(session: dict[str, Any]) -> bool:
    """No other session (shopping or return) started after this visit was paid: the shelf is still its own."""
    conn = db.connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM store_sessions WHERE id != ? AND started_at >= ?",
                            (session["id"], session["approved_at"] or session["started_at"])).fetchone()[0] == 0
    finally:
        conn.close()


def _refund_one(member: dict[str, Any], session: dict[str, Any], payment: dict[str, Any], sku: str,
                dispute_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """One unit plus its tax back to the card through the returns refund path (reason "dispute")."""
    left = returns.remaining(session, payment)
    bought = returns.purchased(session)
    _, refunded_cents = returns.refunded(payment["id"])
    _, _, amount = returns.refund_amount({sku: 1}, left, {s: b["unit_cents"] for s, b in bought.items()},
                                         payment["amount_cents"], refunded_cents, settings.store["tax_rate"])
    attempt = sum(1 for r in payments.payment_refunds(payment["id"]) if r.get("dispute_id") == dispute_id) + 1
    result = payments.refund(payment, amount, dispute_id, attempt, reason="dispute")
    refund = payments.record_refund(payment, None, member, amount,
                                    [{"sku": sku, "name": bought[sku]["name"], "qty": 1}], result,
                                    reason="dispute", dispute_id=dispute_id)
    return refund, result


# --- routes (8.1) ---

class DisputeBody(BaseModel):
    sku: str
    session_id: str | None = None  # a paid visit (receipt "Report a problem"); omit while shopping


@router.post("/api/disputes")
def open_dispute(body: DisputeBody, request: Request):
    """Dispute one unit of `sku`. While shopping (or at the exit) against the cart; with the session_id of a
    paid visit, against the receipt. The camera rechecks first; see the module docstring."""
    from backend import members  # local: members imports routes_api

    require_disputes()
    member = members.current_member(request)
    if body.sku not in settings.skus:
        raise ApiError(400, "unknown_sku", "Unknown item.")
    active = store.current_session()
    with _lock:
        if body.session_id and not (active and active["id"] == body.session_id):
            return _open_after_purchase(member, body.session_id, body.sku)
        session = _own_shopping(member["id"])
        if session is None:
            raise ApiError(409, "no_active_session", "You are not in the store.")
        shown = _qty(store.cart_for(session), body.sku)
        if shown < 1:
            raise ApiError(409, "not_in_cart", "That item is not in your cart any more.")
        stage = CHECKOUT if session["state"] == store.CHECKOUT_PENDING else SHOPPING
        evidence = build_evidence(session["id"], body.sku)
        camera = store.camera_quantities(session).get(body.sku, 0) if _vision_fresh() else None
        if camera is not None and camera < shown:  # the latest stable shelf has the unit back
            cart, amount = _remove_from_cart(session, body.sku)
            row = _insert(session, member["id"], body.sku, stage, RESOLVED_CAMERA, RESOLVED, evidence, amount)
        else:
            cart = store.cart_for(store.get_session(session["id"]))
            row = _insert(session, member["id"], body.sku, stage, NEEDS_REVIEW, OPEN, evidence)
    return {"dispute": view(row), "cart": cart}


def _open_after_purchase(member: dict[str, Any], session_id: str, sku: str) -> dict[str, Any]:
    session, payment = _paid_visit(member["id"], session_id, sku)
    evidence = build_evidence(session_id, sku)
    left = returns.remaining(session, payment).get(sku, 0)
    camera = store.camera_quantities(session).get(sku, 0) \
        if _vision_fresh() and _nobody_since(session) and store.current_session() is None else None
    if camera is None or camera >= left:
        row = _insert(session, member["id"], sku, AFTER_PURCHASE, NEEDS_REVIEW, OPEN, evidence)
        return {"dispute": view(row), "cart": None}
    # The shelf has more of it than this visit left there: the camera got it wrong, refund one unit now.
    row = _insert(session, member["id"], sku, AFTER_PURCHASE, NEEDS_REVIEW, OPEN, evidence)
    refund, result = _refund_one(member, session, payment, sku, row["id"])
    if refund["status"] != payments.REFUND_SUCCEEDED:
        eventlog.log("dispute_refund_failed", dispute_id=row["id"], session_id=session_id, message=result.get("message"))
        return {"dispute": view(row), "cart": None, "refund": refund,
                "message": result.get("message") or "The refund did not go through."}
    row = _settle(row["id"], RESOLVED_CAMERA, RESOLVED, cart_mod.to_cents(refund["amount_usd"]), refund["refund_id"])
    eventlog.log("dispute_refunded", dispute_id=row["id"], session_id=session_id, sku=sku, source="camera",
                 amount_usd=refund["amount_usd"], refund_id=refund["refund_id"])
    return {"dispute": view(row, refund=refund), "cart": None, "refund": refund}


def _own_dispute(dispute_id: str, member_id: str) -> dict[str, Any]:
    row = _row(dispute_id)
    if row is None or row["member_id"] != member_id:
        raise ApiError(404, "not_found", "No dispute here.")
    if row["status"] != OPEN:
        raise ApiError(409, "dispute_closed", "This dispute is already settled.")
    return row


@router.post("/api/disputes/{dispute_id}/keep")
def keep(dispute_id: str, request: Request):
    """"Found it, keep it": nothing changes, the dispute is withdrawn."""
    from backend import members

    require_disputes()
    member = members.current_member(request)
    with _lock:
        row = _settle(_own_dispute(dispute_id, member["id"])["id"], KEPT, WITHDRAWN)
    eventlog.log("dispute_kept", dispute_id=dispute_id, session_id=row["store_session_id"], sku=row["sku"])
    session = _own_shopping(member["id"])
    return {"dispute": view(row), "cart": store.cart_for(session) if session else None}


@router.post("/api/disputes/{dispute_id}/remove")
def remove_anyway(dispute_id: str, request: Request):
    """"Remove anyway": one unit out of the cart with a signed override (source "dispute"), or refunded after
    paying. At most MAX_REMOVE_ANYWAY per visit; after that 409 dispute_limit ("Please ask a staff member")."""
    from backend import members

    require_disputes()
    member = members.current_member(request)
    with _lock:
        row = _own_dispute(dispute_id, member["id"])
        if remove_anyway_used(row["store_session_id"]) >= MAX_REMOVE_ANYWAY:
            eventlog.log("dispute_limit", dispute_id=dispute_id, session_id=row["store_session_id"],
                         member_id=member["id"], limit=MAX_REMOVE_ANYWAY)
            raise ApiError(409, "dispute_limit", STAFF_LINE)
        if row["stage"] == AFTER_PURCHASE:
            session, payment = _paid_visit(member["id"], row["store_session_id"], row["sku"])
            refund, result = _refund_one(member, session, payment, row["sku"], dispute_id)
            if refund["status"] != payments.REFUND_SUCCEEDED:
                eventlog.log("dispute_refund_failed", dispute_id=dispute_id, session_id=session["id"],
                             message=result.get("message"))
                return {"dispute": view(row), "cart": None, "refund": refund,
                        "message": result.get("message") or "The refund did not go through."}
            row = _settle(dispute_id, REFUNDED, RESOLVED, cart_mod.to_cents(refund["amount_usd"]), refund["refund_id"])
            eventlog.log("dispute_refunded", dispute_id=dispute_id, session_id=session["id"], sku=row["sku"],
                         source="remove_anyway", amount_usd=refund["amount_usd"], refund_id=refund["refund_id"])
            return {"dispute": view(row, refund=refund), "cart": None, "refund": refund}
        session = _own_shopping(member["id"])
        if session is None or session["id"] != row["store_session_id"]:
            raise ApiError(409, "no_active_session", "You are not in the store.")
        if _qty(store.cart_for(session), row["sku"]) < 1:
            raise ApiError(409, "not_in_cart", "That item is not in your cart any more.")
        cart, amount = _remove_from_cart(session, row["sku"])
        row = _settle(dispute_id, REMOVED, RESOLVED, amount)
    eventlog.log("dispute_removed", dispute_id=dispute_id, session_id=row["store_session_id"], sku=row["sku"],
                 amount_usd=cart_mod.to_usd(amount), total_usd=cart["total_usd"])
    return {"dispute": view(row), "cart": cart}


def _serve(session_id: str, file: str) -> FileResponse:
    conn = db.connect()
    try:
        known = conn.execute("SELECT 1 FROM evidence WHERE store_session_id = ? AND file = ?",
                             (session_id, file)).fetchone()
    finally:
        conn.close()
    path = evidence_root() / session_id / file
    if not known or not FILE_RE.match(file) or not path.is_file():
        raise ApiError(404, "not_found", "No photo here.")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=600"})


@router.get("/api/disputes/evidence/{session_id}/{file}")
def evidence_photo(session_id: str, file: str, request: Request):
    """A shelf crop, only for the shopper whose visit it is (401 signed out, 404 anyone else)."""
    from backend import members

    require_disputes()
    member = members.current_member(request)
    session = store.get_session(session_id)
    if session is None or session["member_id"] != member["id"]:
        raise ApiError(404, "not_found", "No photo here.")
    return _serve(session_id, file)


@admin_router.get("/admin/evidence/{session_id}/{file}")
def admin_evidence_photo(session_id: str, file: str):
    """The same crops for the admin page's disputes card (team only)."""
    return _serve(session_id, file)


def recent(limit: int = 20) -> list[dict[str, Any]]:
    """Newest disputes for the admin page, photos through the admin route, with the shopper's name."""
    conn = db.connect()
    try:
        rows = conn.execute("SELECT d.*, m.name AS member_name FROM disputes d LEFT JOIN members m "
                            "ON m.id = d.member_id ORDER BY d.created_at DESC, d.rowid DESC LIMIT ?",
                            (limit,)).fetchall()
    finally:
        conn.close()
    return [{**view(dict(r), url_prefix="/admin/evidence"), "member_name": r["member_name"]} for r in rows]


def report_status(session: dict[str, Any]) -> dict[str, Any]:
    """Receipt "Report a problem": can this paid visit still be disputed, and which items."""
    out: dict[str, Any] = {"eligible": False, "reason": None, "message": "", "deadline": None, "items": []}
    if not settings.features.disputes:
        out.update(reason="disputes_off")
        return out
    payment = returns.paid_payment(session["id"]) if not session.get("return_of") else None
    if payment is None or session["state"] not in (store.PAID, store.CLOSED) \
            or payment["provider"] not in returns.REFUNDABLE_PROVIDERS:
        out.update(reason="not_disputable", message="Only a paid visit can be reported.")
        return out
    deadline = _parse(payment["created_at"]) + REPORT_WINDOW
    out["deadline"] = returns._iso(deadline)
    if datetime.now(timezone.utc) > deadline:
        out.update(reason="dispute_window_closed", message=STAFF_LINE)
        return out
    bought = returns.purchased(session)
    out["items"] = [{"sku": sku, "name": bought[sku]["name"], "qty": n}
                    for sku, n in returns.remaining(session, payment).items()]
    out["eligible"] = bool(out["items"])
    if not out["eligible"]:
        out.update(reason="nothing_to_refund", message="Everything from this visit is already refunded.")
    return out


# --- evidence cleanup ---

def _keep_reason(session_id: str, now: datetime) -> str | None:
    """Why this visit's evidence is still kept, or None when it must go."""
    session = store.get_session(session_id) if SESSION_RE.match(session_id) else None
    if session is None:
        return None
    if session["state"] in store.SHOPPING_STATES:
        return "shopping"
    conn = db.connect()
    try:
        last = conn.execute("SELECT MAX(created_at) FROM disputes WHERE store_session_id = ? AND status IN (?, ?)",
                            (session_id, OPEN, RESOLVED)).fetchone()[0]
    finally:
        conn.close()
    if last and now < _parse(last) + EVIDENCE_HOLD:
        return "dispute"
    payment = returns.paid_payment(session_id) if not session.get("return_of") else None
    if payment is not None and not last and now < _parse(payment["created_at"]) + REPORT_WINDOW:
        return "report_window"
    return None


def cleanup_evidence(now: datetime | None = None) -> list[str]:
    """Delete the crops of every visit that is over (see the module docstring). Returns the session ids."""
    now = now or datetime.now(timezone.utc)
    root = evidence_root()
    conn = db.connect()
    try:
        ids = {r[0] for r in conn.execute("SELECT DISTINCT store_session_id FROM evidence")}
    finally:
        conn.close()
    if root.is_dir():
        ids |= {p.name for p in root.iterdir() if p.is_dir()}
    deleted = []
    for session_id in sorted(ids):
        if _keep_reason(session_id, now):
            continue
        folder = root / session_id
        files = len(list(folder.glob("*.jpg"))) if folder.is_dir() else 0
        shutil.rmtree(folder, ignore_errors=True)
        conn = db.connect()
        try:
            conn.execute("DELETE FROM evidence WHERE store_session_id = ?", (session_id,))
            conn.commit()
        finally:
            conn.close()
        eventlog.log("evidence_deleted", session_id=session_id, files=files)
        deleted.append(session_id)
    return deleted


async def cleanup_task() -> None:
    """Background loop started by main.py's lifespan: evidence goes within CLEANUP_SECONDS of the visit ending."""
    while True:
        try:
            await asyncio.to_thread(cleanup_evidence)
        except Exception as e:  # never let the cleaner die
            eventlog.log("evidence_cleanup_error", error=repr(e))
        await asyncio.sleep(CLEANUP_SECONDS)
