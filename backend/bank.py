"""Demo bank (8.15): the simulated bank account behind every member's demo card.

A ledger in SQLite (`bank_ledger`), one row per movement, all money in integer cents:

    opening      +  the opening balance (config.json demo_bank.opening_balance_usd), created with the member
    charge       -  a paid visit (payments.record, status AUTHORIZED)
    refund       +  a return / dispute / staff refund (payments.record_refund, status SUCCEEDED)
    top_up       +  "Add $20 demo funds", at most demo_bank.max_top_ups per member
    hold         -  a pre-authorization hold, status pending (nothing in this build issues one: charges are
    hold_release +  immediate, so these types are only carried by the math and never appear)

Current balance = the posted rows. Available balance = current minus the pending holds. Nothing here is a real
account: every balance view says so, and the approve step declines a cart that does not fit the available
balance ("Insufficient funds on your demo card"). Every change pushes {"type":"bank"} over the WebSocket.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from backend import db, eventlog, ws
from backend.settings import settings

CARD_LABEL = "Demo Visa •••• 4242"
NOTE = "Demo balance · not a real account"
OPENING, HOLD, HOLD_RELEASE, CHARGE, REFUND, TOP_UP = "opening", "hold", "hold_release", "charge", "refund", "top_up"
TYPES = (OPENING, HOLD, HOLD_RELEASE, CHARGE, REFUND, TOP_UP)
PENDING, POSTED = "pending", "posted"
RECENT = 20  # transactions in GET /api/bank and the socket message


class BankError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _cents(usd: float | int) -> int:
    return int(round(float(usd) * 100))


def _usd(cents: int) -> float:
    return round(cents / 100, 2)


def opening_balance_cents() -> int:
    return _cents(settings.demo_bank["opening_balance_usd"])


def top_up_cents() -> int:
    return _cents(settings.demo_bank["top_up_usd"])


def max_top_ups() -> int:
    return int(settings.demo_bank["max_top_ups"])


# --- ledger ---

def _insert(conn: sqlite3.Connection, member_id: str, kind: str, amount_cents: int, description: str,
            related_id: str | None = None, status: str = POSTED) -> int:
    if kind not in TYPES:
        raise ValueError(f"unknown ledger type {kind!r}")
    cur = conn.execute(
        "INSERT INTO bank_ledger (member_id, type, amount_cents, status, description, related_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (member_id, kind, int(amount_cents), status, description, related_id, db.now_iso()))
    return int(cur.lastrowid)


def _has_opening(conn: sqlite3.Connection, member_id: str) -> bool:
    return conn.execute("SELECT 1 FROM bank_ledger WHERE member_id = ? AND type = ? LIMIT 1",
                        (member_id, OPENING)).fetchone() is not None


def _ensure_opening(conn: sqlite3.Connection, member_id: str) -> bool:
    """The opening balance row, if this member has none yet (signup, backfill, or a member made by hand)."""
    if _has_opening(conn, member_id):
        return False
    if conn.execute("SELECT 1 FROM members WHERE id = ?", (member_id,)).fetchone() is None:
        return False  # a cookie for a member that no longer exists (DB reset): no account to open
    _insert(conn, member_id, OPENING, opening_balance_cents(), "Opening demo balance")
    return True


def open_account(member_id: str) -> None:
    """Signup: the opening balance. Idempotent."""
    conn = db.connect()
    try:
        created = _ensure_opening(conn, member_id)
        conn.commit()
    finally:
        conn.close()
    if created:
        eventlog.log("bank_opened", member_id=member_id, opening_usd=_usd(opening_balance_cents()))


def backfill() -> list[str]:
    """Startup: every member without an opening row (older members, the seeded demo member) gets one."""
    conn = db.connect()
    try:
        members = [r["id"] for r in conn.execute("SELECT id FROM members ORDER BY created_at, rowid")]
        done = [m for m in members if _ensure_opening(conn, m)]
        conn.commit()
    finally:
        conn.close()
    if done:
        eventlog.log("bank_backfill", members=done, opening_usd=_usd(opening_balance_cents()))
    return done


def balances_cents(member_id: str, conn: sqlite3.Connection | None = None) -> tuple[int, int]:
    """(current, available): current = posted rows; available = current minus pending holds."""
    own = conn is None
    conn = conn or db.connect()
    try:
        _ensure_opening(conn, member_id)
        if own:
            conn.commit()
        posted = conn.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM bank_ledger WHERE member_id = ? AND status = ?",
                              (member_id, POSTED)).fetchone()[0]
        holds = conn.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM bank_ledger WHERE member_id = ? "
                             "AND status = ? AND type = ?", (member_id, PENDING, HOLD)).fetchone()[0]
        return int(posted), int(posted) + int(holds)  # holds are negative rows
    finally:
        if own:
            conn.close()


def available_cents(member_id: str) -> int:
    return balances_cents(member_id)[1]


def top_ups_used(member_id: str, conn: sqlite3.Connection | None = None) -> int:
    own = conn is None
    conn = conn or db.connect()
    try:
        return int(conn.execute("SELECT COUNT(*) FROM bank_ledger WHERE member_id = ? AND type = ?",
                                (member_id, TOP_UP)).fetchone()[0])
    finally:
        if own:
            conn.close()


def _row_view(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "type": row["type"],
        "amount_usd": _usd(row["amount_cents"]),
        "status": row["status"],
        "description": row["description"],
        "related_id": row["related_id"],
        "created_at": row["created_at"],
    }


def summary(member_id: str) -> dict[str, Any]:
    """What GET /api/bank returns and the `bank` socket message carries (8.15)."""
    conn = db.connect()
    try:
        current, available = balances_cents(member_id, conn)
        rows = conn.execute("SELECT * FROM bank_ledger WHERE member_id = ? ORDER BY id DESC LIMIT ?",
                            (member_id, RECENT)).fetchall()
        used = top_ups_used(member_id, conn)
        conn.commit()
    finally:
        conn.close()
    return {
        "card_label": CARD_LABEL,
        "note": NOTE,
        "balance_usd": _usd(current),
        "available_usd": _usd(available),
        "pending_usd": _usd(current - available),
        "opening_balance_usd": _usd(opening_balance_cents()),
        "top_up_usd": _usd(top_up_cents()),
        "top_ups_used": used,
        "top_ups_left": max(0, max_top_ups() - used),
        "transactions": [_row_view(r) for r in rows],
    }


def balance_after(member_id: str, related_id: str) -> float | None:
    """The posted balance right after the row tied to `related_id` (a payment id): the receipt's
    "Balance after this purchase". None when no ledger row carries that id (paid before the demo bank existed)."""
    conn = db.connect()
    try:
        row = conn.execute("SELECT id FROM bank_ledger WHERE member_id = ? AND related_id = ? AND type = ? "
                           "ORDER BY id LIMIT 1", (member_id, related_id, CHARGE)).fetchone()
        if row is None:
            return None
        total = conn.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM bank_ledger WHERE member_id = ? "
                             "AND status = ? AND id <= ?", (member_id, POSTED, row["id"])).fetchone()[0]
        return _usd(int(total))
    finally:
        conn.close()


def publish(member_id: str) -> dict[str, Any]:
    """Push the member's bank summary to their phone and the admin sockets (8.4 `bank`)."""
    data = summary(member_id)
    ws.manager.publish({"type": "bank", "data": data}, member_id=member_id, admin=True)
    return data


# --- hooks from the payment flows ---

def item_summary(items: list[dict[str, Any]]) -> str:
    """"SpeedMart #01 · 2 items": the charge's line on the statement."""
    count = sum(int(i.get("qty", 0)) for i in items)
    return f"{settings.store['name']} · {count} item{'s' if count != 1 else ''}"


def post_charge(member_id: str, payment_id: str, amount_cents: int, items: list[dict[str, Any]]) -> dict[str, Any]:
    """An AUTHORIZED charge: a negative posted row. A pending hold for the same payment (none in this build)
    would be released here, turning into the captured charge plus a hold_release."""
    conn = db.connect()
    try:
        _ensure_opening(conn, member_id)
        hold = conn.execute("SELECT id, amount_cents FROM bank_ledger WHERE member_id = ? AND related_id = ? "
                            "AND type = ? AND status = ?", (member_id, payment_id, HOLD, PENDING)).fetchone()
        if hold is not None:
            conn.execute("UPDATE bank_ledger SET status = ? WHERE id = ?", (POSTED, hold["id"]))
            _insert(conn, member_id, HOLD_RELEASE, -int(hold["amount_cents"]), "Hold released", payment_id)
        _insert(conn, member_id, CHARGE, -abs(int(amount_cents)), item_summary(items), payment_id)
        current, available = balances_cents(member_id, conn)
        conn.commit()
    finally:
        conn.close()
    eventlog.log("bank_charge", member_id=member_id, payment_id=payment_id, amount_usd=_usd(amount_cents),
                 balance_usd=_usd(current), available_usd=_usd(available))
    return publish(member_id)


def post_refund(member_id: str, refund_id: str, amount_cents: int, items_text: str, reason: str) -> dict[str, Any]:
    """A SUCCEEDED refund: a positive posted row ("Refund · 1 Hydration drink")."""
    label = "Refund" if reason == "return" else "Refund (reported problem)"
    conn = db.connect()
    try:
        _ensure_opening(conn, member_id)
        _insert(conn, member_id, REFUND, abs(int(amount_cents)), f"{label} · {items_text}" if items_text else label,
                refund_id)
        current, available = balances_cents(member_id, conn)
        conn.commit()
    finally:
        conn.close()
    eventlog.log("bank_refund", member_id=member_id, refund_id=refund_id, reason=reason,
                 amount_usd=_usd(amount_cents), balance_usd=_usd(current), available_usd=_usd(available))
    return publish(member_id)


def check_funds(member_id: str, total_cents: int, session_id: str | None = None) -> None:
    """The approve step: the cart must fit the available balance, else a friendly decline (logged)."""
    current, available = balances_cents(member_id)
    if total_cents <= available:
        return
    eventlog.log("bank_insufficient_funds", member_id=member_id, session_id=session_id,
                 total_usd=_usd(total_cents), balance_usd=_usd(current), available_usd=_usd(available))
    raise BankError("insufficient_funds",
                    f"Insufficient funds on your demo card (demo balance ${_usd(available):.2f}). "
                    "Put something back, or add demo funds on your home page.")


def top_up(member_id: str) -> dict[str, Any]:
    """"Add $20 demo funds": a positive posted row, at most demo_bank.max_top_ups per member."""
    conn = db.connect()
    try:
        _ensure_opening(conn, member_id)
        used = top_ups_used(member_id, conn)
        if used >= max_top_ups():
            conn.commit()
            eventlog.log("bank_topup_limit", member_id=member_id, top_ups_used=used)
            raise BankError("topup_limit", f"You've used all {max_top_ups()} demo top ups for this account.")
        _insert(conn, member_id, TOP_UP, top_up_cents(), "Demo top up")
        current, available = balances_cents(member_id, conn)
        conn.commit()
    finally:
        conn.close()
    eventlog.log("bank_topup", member_id=member_id, amount_usd=_usd(top_up_cents()), top_ups_used=used + 1,
                 balance_usd=_usd(current), available_usd=_usd(available))
    return publish(member_id)


def reset_all() -> list[str]:
    """Admin "Reset demo balances": every member back to the opening balance, top ups available again."""
    conn = db.connect()
    try:
        members = [r["id"] for r in conn.execute("SELECT id FROM members ORDER BY created_at, rowid")]
        conn.execute("DELETE FROM bank_ledger")
        for member_id in members:
            _insert(conn, member_id, OPENING, opening_balance_cents(), "Opening demo balance")
        conn.commit()
    finally:
        conn.close()
    eventlog.log("bank_reset", members=len(members), opening_usd=_usd(opening_balance_cents()))
    for member_id in members:
        publish(member_id)
    return members
