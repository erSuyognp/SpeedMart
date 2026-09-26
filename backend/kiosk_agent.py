"""Entrance kiosk agent: the tablet at the door guides shoppers, first timers especially (docs/voice.md, "Kiosk").

This module decides what the kiosk says and when; backend/kiosk.py serves it and backend/tts.py voices it.

- The spoken tour (Join, Enter, Grab items, Scan exit) and the fixed phrases, worded for the current feature flags.
- Visit events for the kiosk's step tracker: {"type": "kiosk_visit"} on kiosk sockets only (8.4), taken from the
  store's own session_state log lines, so nothing in store.py needs to know the kiosk exists.
- "shelf_activity" (public, no data): motion at the shelf while the store is empty, so the kiosk can offer the tour.
- The shopper in the store (kiosk sockets and GET /api/kiosk/shopper only): first name, budget, what is left, cart
  total, item count and visit count, plus a greeting (fuller on a first visit).
- Narration: short spoken lines about cart changes, debounced 1.5 s, chosen from the agent policy's decisions (7.4)
  and the cart's own totals, so a price is never invented. {"type": "kiosk_say"} on kiosk sockets.
- The kiosk conversation (features.voice): which shopper the kiosk is talking with, so the phone's voice waits
  ({"type": "kiosk_voice"} to that shopper's sockets) and the conversation ends with the visit.

Everything here is decoration on top of the core loop: every hook is wrapped so a failure is logged, never raised.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from backend import agent, db, eventlog, tts, ws
from backend.cart import to_cents, to_usd
from backend.settings import settings

SHOPPING_STATES = ("IN_STORE", "CHECKOUT_PENDING")
STEP_FOR_STATE = {"IN_STORE": 3, "CHECKOUT_PENDING": 4}  # 1 Join, 2 Enter, 3 Grab items, 4 Scan exit
SHELF_ACTIVITY_MIN_INTERVAL_S = 30.0  # at most one public shelf_activity message per 30 s (the kiosk offers
                                      # the tour at most once every 2 minutes on top of that)
DEBOUNCE_S = 1.5  # a cart change is narrated once the cart has been still this long; the line describes the net
                  # change, so a pick and a put back inside the window say nothing
EXIT_REMINDER_IDLE_S = 60.0  # items in the cart and no change for this long: say how to check out (once)


# --- wording that follows the feature flags ---

def exit_action() -> str:
    """How this build's shopper checks out: the exit QR (gates on) or the Checkout button (gates off)."""
    return "scan the exit code" if settings.features.gates else "tap Checkout on your phone"


def approve_with() -> str:
    return "Face ID" if settings.features.passkeys else "a tap on Confirm"


def tour_lines() -> list[dict[str, Any]]:
    """The spoken tour, about 30 seconds. `step` is the step highlighted while the line plays (0: none)."""
    if not settings.features.signup:
        join = "Step one, join. Ask the team for the demo shopper account."
    elif settings.features.passkeys:
        join = "Step one, join. Scan the join code, type your first name, and save a passkey with Face ID."
    else:
        join = "Step one, join. Scan the join code and type your first name."
    if settings.features.gates:
        enter = f"Step two, enter. Scan the enter code and confirm with {approve_with()}. One shopper at a time."
    else:
        enter = "Step two, enter. Tap Start shopping on your phone. One shopper at a time."
    return [
        {"step": 0, "text": "SpeedMart is a store where the shelf camera builds your cart, "
                            "and you approve the payment with your phone."},
        {"step": 1, "text": join},
        {"step": 2, "text": enter},
        {"step": 3, "text": "Step three, grab what you want. The camera adds it to your cart. "
                            "Put it back, and it comes off."},
        {"step": 4, "text": f"Step four, {exit_action()}, check your cart, and approve with {approve_with()}. "
                            "That's it!"},
    ]


def phrases() -> dict[str, str]:
    """Fixed lines outside the tour (no numbers, so they are cached as files)."""
    return {
        "offer": "Hi! New here? Tap the screen, and I'll show you how SpeedMart works.",
        "exit_reminder": f"When you're ready, {exit_action()} to review and pay.",
    }


def _money(usd: float) -> str:
    """$2.70, $10: the phone agent's formatting (agent.fmt_usd), so both say prices the same way."""
    return "$" + agent.fmt_usd(usd)


def _join(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def say_item(kind: str, text: str, **extra: Any) -> dict[str, Any]:
    """One line for the kiosk: the text (always shown as a caption) and where to fetch its audio, if speech is on.
    The audio URL names a clip id; the kiosk adds its token (?k=) to fetch it."""
    audio_url = f"/api/kiosk/tts/{tts.register(text)}" if tts.available() else None
    return {"kind": kind, "text": text, "audio_url": audio_url, **extra}


def script() -> dict[str, Any]:
    """GET /api/kiosk/phrases: the tour and the fixed phrases, with audio URLs. Warms the TTS file cache."""
    tour = [say_item("tour", line["text"], step=line["step"]) for line in tour_lines()]
    fixed = {key: say_item(key, text) for key, text in phrases().items()}
    tts.warm([t["text"] for t in tour] + [p["text"] for p in fixed.values()])
    # speech: lines come with audio. voice: ElevenLabs is on, so the Start tap also asks for the microphone.
    # conversation: an agent is configured, so the kiosk talks with each shopper who walks in.
    return {"speech": tts.available(), "voice": settings.features.voice, "conversation": conversation_available(),
            "tour": tour, "phrases": fixed}


def conversation_available() -> bool:
    env = settings.env
    return bool(settings.features.voice and env.elevenlabs_api_key and env.elevenlabs_agent_id)


# --- the shopper in the store (kiosk only) ---

def first_name(name: str | None) -> str:
    return (name or "").strip().split(" ")[0] or "there"


def visit_count(member_id: str) -> int:
    """Shopping visits this member has made, this one included. A return is not a visit, and a cancelled visit
    (admin reset, timeout) does not count, so a first timer whose first try was reset is still greeted as new."""
    conn = db.connect()
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM store_sessions WHERE member_id = ? AND return_of IS NULL "
                           "AND state != 'CANCELLED'", (member_id,)).fetchone()
        return int(row["n"])
    finally:
        conn.close()


def shopper_view(cart: dict[str, Any], member: dict[str, Any], visits: int) -> dict[str, Any]:
    """Everything the public screen may know about the shopper, and nothing more: first name, budget, what is left,
    cart total, item count and visit count. Never a card, a balance, a receipt or the items themselves."""
    budget = to_cents(cart.get("budget_usd", member.get("budget_usd", 0)))
    total = to_cents(cart.get("total_usd", 0))
    return {
        "first_name": first_name(member.get("name")),
        "budget_usd": to_usd(budget),
        "remaining_usd": to_usd(max(0, budget - total)),
        "cart_total_usd": to_usd(total),
        "item_count": sum(int(i["qty"]) for i in cart.get("items") or []),
        "visit_count": visits,
    }


def greeting(view: dict[str, Any]) -> tuple[str, str]:
    """(kind, text): a fuller welcome on the first visit (no numbers, so cached as a file), a short one after."""
    name = view["first_name"]
    if view["visit_count"] <= 1:
        return "greeting_first", (f"Welcome to your first visit, {name}. Just grab what you want; the camera adds "
                                  f"it to your cart. When you're done, {exit_action()}.")
    return "greeting_returning", f"Welcome back, {name}. You have {_money(view['remaining_usd'])} to spend."


def current_visit() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None:
    """(session, cart, member) for the shopping visit holding the store, or None (free, or a return)."""
    from backend import members, store  # local: both import modules that import this one

    session = store.current_session()
    if session is None or session.get("return_of") or session["state"] not in SHOPPING_STATES:
        return None
    member = members.get_member(session["member_id"])
    if member is None:
        return None
    return session, store.cart_for(session), member


def shopper_payload() -> dict[str, Any]:
    """GET /api/kiosk/shopper: {"shopper": view, "greeting": Say}, or both null while nobody is shopping."""
    visit = current_visit()
    if visit is None:
        return {"shopper": None, "greeting": None}
    session, cart, member = visit
    view = shopper_view(cart, member, visit_count(member["id"]))
    kind, text = greeting(view)
    return {"shopper": view, "greeting": say_item(kind, text, visit=True)}


def cart_message(view: dict[str, Any]) -> dict[str, Any]:
    return {"type": "kiosk_cart", "data": view}


# --- narration: what the kiosk says about cart changes ---

def _item_names(changes: list[tuple[str, int]]) -> tuple[str, bool]:
    """[("Chips", 1), ("Water", 2)] -> ("Chips and 2 Waters", plural)."""
    parts = [name if n == 1 else f"{n} {name}" + ("" if name.endswith("s") else "s") for name, n in changes]
    plural = len(changes) > 1 or changes[0][1] > 1 or changes[0][0].lower().endswith("s")
    return _join(parts), plural


def _over_budget(cart: dict[str, Any], member: dict[str, Any]) -> dict[str, Any] | None:
    """The policy's over_budget decision for this cart, even while a misplaced warning outranks it (7.4)."""
    decision = agent.policy({**cart, "warnings": []}, member)
    return decision if decision["kind"] == "over_budget" else None


def narration_line(prev: dict[str, Any], cart: dict[str, Any], member: dict[str, Any]) -> tuple[str, str] | None:
    """(kind, text) for the net change from `prev` to `cart`, or None when there is nothing worth saying.

    First match wins: a new misplaced item, then over budget (on a pick, on crossing the budget, or a new amount
    over), then picks, then put backs. Every price comes from the cart or the policy, never from here.
    """
    before = {i["sku"]: int(i["qty"]) for i in prev.get("items") or []}
    now = {i["sku"]: int(i["qty"]) for i in cart.get("items") or []}
    names = {sku: s.name for sku, s in settings.skus.items()}
    added = [(names[s], now[s] - before.get(s, 0)) for s in settings.skus if now.get(s, 0) > before.get(s, 0)]
    removed = [(names[s], before[s] - now.get(s, 0)) for s in settings.skus if before.get(s, 0) > now.get(s, 0)]

    decision = agent.policy(cart, member)
    seen = {(w.get("sku"), w.get("bay")) for w in prev.get("warnings") or []}
    if decision["kind"] == "misplaced" and (decision["sku"], decision["bay"]) not in seen:
        return "misplaced", (f"Oops, the {decision['name']} is in the wrong bay. "
                             f"Please put it back in bay {decision['return_to_bay']}.")

    over, was_over = _over_budget(cart, member), _over_budget(prev, member)
    if over and (added or was_over is None or to_cents(was_over["over_by"]) != to_cents(over["over_by"])):
        return "over_budget", (f"That's {_money(over['over_by'])} over your budget. "
                               f"Putting back the {over['put_back_name']} fixes it.")
    if added:
        text, plural = _item_names(added)
        return "pick", (f"See, the {text} {'are' if plural else 'is'} already in your cart. "
                        f"You're at {_money(cart['total_usd'])}.")
    if removed:
        text, plural = _item_names(removed)
        line = f"{text} {'are' if plural else 'is'} back on the shelf, removed from your cart."
        if was_over and not over:
            line += " You're back under your budget."
        return "put_back", line
    return None


def _say(kind: str, text: str) -> dict[str, Any]:
    return {"type": "kiosk_say", "data": say_item(kind, text, visit=True)}


class Narrator:
    """Turns cart changes into kiosk lines: one debounced line per burst of changes, an immediate panel update
    (kiosk_cart) for every change, and the exit reminder after EXIT_REMINDER_IDLE_S without a change.

    Inputs come from the eventlog subscriber and never block; the work (DB reads, choosing a line) runs on one
    daemon thread. Tests pass threaded=False and drive run_due(now) with their own clock and cart source.
    """

    def __init__(self, *, debounce_s: float = DEBOUNCE_S, idle_s: float = EXIT_REMINDER_IDLE_S,
                 clock=time.monotonic, current=None, emit=None, threaded: bool = True) -> None:
        self.debounce_s = debounce_s
        self.idle_s = idle_s
        self.clock = clock
        self._current = current or current_visit
        self._emit = emit or ws.broadcast_kiosk
        self.threaded = threaded
        self._cond = threading.Condition()
        self._dirty = False
        self._thread: threading.Thread | None = None
        self._reset_locked(None)

    def _reset_locked(self, session_id: str | None) -> None:
        self._session_id = session_id
        self._active = session_id is not None  # IN_STORE: narrate. Paused at the exit, stopped when it ends.
        self._last: dict[str, Any] | None = None  # the cart the last line described (None: take a baseline)
        self._panel_due: float | None = None
        self._say_due: float | None = None
        self._idle_due: float | None = None

    # --- inputs (called inside eventlog.log: must not block) ---

    def visit_started(self, session_id: str) -> None:
        with self._cond:
            self._reset_locked(session_id)
            self._panel_due = self.clock()
            self._wake_locked()

    def visit_paused(self, session_id: str | None = None) -> None:
        """Exit scanned: the cart is frozen, the kiosk stops talking about it (the panel still updates)."""
        with self._cond:
            if session_id is None or session_id == self._session_id:
                self._active = False
                self._say_due = self._idle_due = None
                self._wake_locked()

    def visit_resumed(self, session_id: str) -> None:
        """Keep shopping: narrate again, and the exit reminder comes back if the cart still has items."""
        with self._cond:
            if session_id != self._session_id:
                self._reset_locked(session_id)
            self._active = True
            self._panel_due = self.clock()
            if self._last and self._last.get("items"):
                self._idle_due = self.clock() + self.idle_s
            self._wake_locked()

    def visit_ended(self) -> None:
        with self._cond:
            self._reset_locked(None)
            self._wake_locked()

    def cart_changed(self, session_id: str | None, state: str | None = None) -> None:
        with self._cond:
            if session_id != self._session_id:
                if state != "IN_STORE" or not session_id:
                    return
                self._reset_locked(session_id)  # a visit already under way (e.g. after a backend restart)
            now = self.clock()
            self._panel_due = now
            if self._active:
                self._say_due = now + self.debounce_s  # every change pushes the line back: latest cart wins
                self._idle_due = None
            self._wake_locked()

    # --- work ---

    def _wake_locked(self) -> None:
        self._dirty = True
        if self.threaded and (self._thread is None or not self._thread.is_alive()):
            self._thread = threading.Thread(target=self._run, name="kiosk-narrator", daemon=True)
            self._thread.start()
        self._cond.notify_all()

    def _next_wait_locked(self, now: float) -> float | None:
        dues = [d for d in (self._panel_due, self._say_due, self._idle_due) if d is not None]
        return max(0.0, min(dues) - now) if dues else None

    def run_due(self, now: float | None = None) -> float | None:
        """Do whatever is due at `now`. Returns seconds until the next due time, or None when nothing is pending."""
        now = self.clock() if now is None else now
        with self._cond:
            session_id = self._session_id
            panel = self._panel_due is not None and self._panel_due <= now
            say = self._active and self._say_due is not None and self._say_due <= now
            idle = self._active and self._idle_due is not None and self._idle_due <= now
            if panel:
                self._panel_due = None
            if say:
                self._say_due = None
            if idle:
                self._idle_due = None
            baseline = self._last is None
            if session_id is None or not (panel or say or idle):
                return self._next_wait_locked(now)

        visit = self._current()
        if visit is None or visit[0]["id"] != session_id:
            with self._cond:
                return self._next_wait_locked(now)
        session, cart, member = visit
        if panel:
            self._emit(cart_message(shopper_view(cart, member, visit_count(member["id"]))))

        line = None
        with self._cond:
            if session_id != self._session_id:  # the visit ended while we read the cart
                return self._next_wait_locked(now)
            if baseline:
                self._last = cart  # the cart at entry (or when we joined a visit): nothing to say about it
            elif say:
                line = narration_line(self._last, cart, member)
                self._last = cart
            if (baseline or say) and self._active and cart.get("items"):
                self._idle_due = now + self.idle_s
            remind = idle and self._active and session["state"] == "IN_STORE" and bool(cart.get("items"))
            wait = self._next_wait_locked(now)
        if line is not None:
            self._emit(_say(*line))
            eventlog.log("kiosk_say", session_id=session_id, kind=line[0])
        if remind:
            self._emit(_say("exit_reminder", phrases()["exit_reminder"]))
            eventlog.log("kiosk_say", session_id=session_id, kind="exit_reminder")
        return wait

    def _run(self) -> None:
        while True:
            try:
                wait = self.run_due()
            except Exception as e:  # never let the narrator thread die
                eventlog.log("kiosk_error", where="narrator", error=repr(e)[:300])
                wait = 1.0
            with self._cond:
                if not self._dirty:
                    self._cond.wait(wait)
                self._dirty = False


narrator = Narrator()


# --- the kiosk conversation: an ElevenLabs agent session with the shopper in the store, one at a time ---

MAX_CONVERSATION_S = 300.0  # the kiosk caps a conversation at 5 minutes
SILENCE_S = 120.0  # and ends it after 2 minutes of silence
CONVERSATION_GRACE_S = 30.0  # a kiosk that died mid-conversation stops blocking the phone after cap + grace

_conv_lock = threading.Lock()
_conversation: dict[str, Any] | None = None  # {"member_id", "session_id", "since": time.monotonic()}


def voice_message(active: bool) -> dict[str, Any]:
    """To the shopper's own sockets: the phone shows "Talk to the kiosk" (disabled) while this is true."""
    return {"type": "kiosk_voice", "data": {"active": active}}


def conversation_active_for(member_id: str | None, now: float | None = None) -> bool:
    """True while the kiosk is in a conversation with this member (the phone's voice waits meanwhile)."""
    now = time.monotonic() if now is None else now
    with _conv_lock:
        c = _conversation
        return (c is not None and member_id is not None and c["member_id"] == member_id
                and now - c["since"] < MAX_CONVERSATION_S + CONVERSATION_GRACE_S)


def start_conversation(session_id: str, member_id: str, now: float | None = None) -> None:
    """The kiosk connected a conversation with this visit's shopper."""
    global _conversation
    now = time.monotonic() if now is None else now
    with _conv_lock:
        previous = _conversation
        _conversation = {"member_id": member_id, "session_id": session_id, "since": now}
    if previous is not None and previous["member_id"] != member_id:
        ws.manager.publish(voice_message(False), member_id=previous["member_id"])
    ws.manager.publish(voice_message(True), member_id=member_id)
    eventlog.log("kiosk_conversation", session_id=session_id, member_id=member_id, active=True)


def end_conversation(reason: str, now: float | None = None) -> bool:
    """The kiosk ended its conversation (silence, 5 minute cap, exit scan, error) or the visit is over. Returns
    False when there was none."""
    global _conversation
    now = time.monotonic() if now is None else now
    with _conv_lock:
        ended, _conversation = _conversation, None
    if ended is None:
        return False
    ws.manager.publish(voice_message(False), member_id=ended["member_id"])
    eventlog.log("kiosk_conversation", session_id=ended["session_id"], member_id=ended["member_id"], active=False,
                 reason=reason, duration_s=round(now - ended["since"], 1))
    return True


# --- visit events for the step tracker (kiosk sockets only) ---

def visit_message(event: str, step: int | None) -> dict[str, Any]:
    return {"type": "kiosk_visit", "data": {"event": event, "step": step}}


def visit_event(entry: dict[str, Any]) -> tuple[str, int | None] | None:
    """(event, step) for a session_state log line, or None when the kiosk does not care (returns, restarts)."""
    if entry.get("type") != "session_state" or entry.get("return_of"):
        return None
    before, after = entry.get("from"), entry.get("to")
    if before is None and after == "IN_STORE":
        return "entered", 3
    if before == "IN_STORE" and after == "CHECKOUT_PENDING":
        return "exit_pending", 4
    if before == "CHECKOUT_PENDING" and after == "IN_STORE":
        return "resumed", 3
    if after == "PAID":
        return "paid", None
    if before in (*SHOPPING_STATES, "PAID") and after in ("CLOSED", "CANCELLED"):
        return "ended", None
    return None


def _on_event(entry: dict[str, Any]) -> None:
    """eventlog subscriber: must not block (it runs inside eventlog.log on the caller's thread)."""
    if entry.get("type") == "cart_changed":
        narrator.cart_changed(entry.get("session_id"), entry.get("state"))
        return
    found = visit_event(entry)
    if found is None:
        return
    event, step = found
    ws.broadcast_kiosk(visit_message(event, step))
    if event == "entered":
        narrator.visit_started(entry["session_id"])
    elif event == "exit_pending":
        narrator.visit_paused(entry.get("session_id"))
    elif event == "resumed":
        narrator.visit_resumed(entry["session_id"])
    else:  # paid, ended
        narrator.visit_ended()
    if event in ("exit_pending", "paid", "ended"):  # the kiosk hangs up too; this frees the phone at once
        end_conversation(reason=event)


def initial_messages() -> list[dict[str, Any]]:
    """What a (re)connecting kiosk socket needs to catch up: the visit in progress and its panel, if any."""
    visit = current_visit()
    if visit is None:
        return []
    session, cart, member = visit
    return [visit_message("sync", STEP_FOR_STATE[session["state"]]),
            cart_message(shopper_view(cart, member, visit_count(member["id"])))]


# --- shelf activity: someone near the shelf while the store is empty ---

_activity_lock = threading.Lock()
_last_activity_at: float | None = None


def note_shelf_motion(any_motion: bool, occupied: bool, now: float | None = None) -> bool:
    """Called for every shelf snapshot. Sends the public shelf_activity message (no data) when a bay reports
    motion while nobody is shopping, at most once per SHELF_ACTIVITY_MIN_INTERVAL_S. Returns True if sent."""
    global _last_activity_at
    if not any_motion or occupied:
        return False
    now = time.monotonic() if now is None else now
    with _activity_lock:
        if _last_activity_at is not None and now - _last_activity_at < SHELF_ACTIVITY_MIN_INTERVAL_S:
            return False
        _last_activity_at = now
    ws.broadcast_shelf_activity()
    eventlog.log("shelf_activity")
    return True


def reset() -> None:
    """Forget throttles and state (tests)."""
    global _last_activity_at, _conversation
    with _activity_lock:
        _last_activity_at = None
    with _conv_lock:
        _conversation = None
    narrator.visit_ended()


_subscribed = False


def _subscribe_once() -> None:
    global _subscribed
    if not _subscribed:
        eventlog.subscribe(_on_event)
        _subscribed = True


_subscribe_once()
