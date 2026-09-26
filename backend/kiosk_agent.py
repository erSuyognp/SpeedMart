"""Entrance kiosk agent: the tablet at the door guides shoppers, first timers especially (docs/voice.md, "Kiosk").

This module decides what the kiosk says and when; backend/kiosk.py serves it and backend/tts.py voices it.

- The spoken tour (Join, Enter, Grab items, Scan exit) and the fixed phrases, worded for the current feature flags.
- Visit events for the kiosk's step tracker: {"type": "kiosk_visit"} on kiosk sockets only (8.4), taken from the
  store's own session_state log lines, so nothing in store.py needs to know the kiosk exists.
- "shelf_activity" (public, no data): motion at the shelf while the store is empty, so the kiosk can offer the tour.

Everything here is decoration on top of the core loop: every hook is wrapped so a failure is logged, never raised.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from backend import eventlog, tts, ws
from backend.settings import settings

SHOPPING_STATES = ("IN_STORE", "CHECKOUT_PENDING")
STEP_FOR_STATE = {"IN_STORE": 3, "CHECKOUT_PENDING": 4}  # 1 Join, 2 Enter, 3 Grab items, 4 Scan exit
SHELF_ACTIVITY_MIN_INTERVAL_S = 30.0  # at most one public shelf_activity message per 30 s (the kiosk offers
                                      # the tour at most once every 2 minutes on top of that)


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
    return {"speech": tts.available(), "voice": settings.features.voice, "tour": tour, "phrases": fixed}


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
    found = visit_event(entry)
    if found is not None:
        ws.broadcast_kiosk(visit_message(*found))


def initial_messages() -> list[dict[str, Any]]:
    """What a (re)connecting kiosk socket needs to catch up: the visit in progress, if any."""
    from backend import store  # local: store imports modules that import this one

    session = store.current_session()
    if session is None or session.get("return_of") or session["state"] not in SHOPPING_STATES:
        return []
    return [visit_message("sync", STEP_FOR_STATE[session["state"]])]


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
    global _last_activity_at
    with _activity_lock:
        _last_activity_at = None


_subscribed = False


def _subscribe_once() -> None:
    global _subscribed
    if not _subscribed:
        eventlog.subscribe(_on_event)
        _subscribed = True


_subscribe_once()
