"""AI store agent line (F11, 7.4 + 9.6).

A deterministic policy decides what to say; the LLM only phrases it. The template line is always computed
first and is what the shopper sees whenever the LLM is off, has no key, times out, or says something that
fails validation.

store.compute_live_cart() hands every recomputed cart to on_cart(). on_cart() never blocks: it attaches the
last line generated for that session to the snapshot and, if the cart content changed, schedules a new line
on the agent thread with a 1.2 s debounce (latest cart wins). When a line is ready it is stored for the next
snapshot and pushed as {"type":"agent","data":{"line":...}}.
"""

from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
from typing import Any

import httpx

from backend import eventlog, ws
from backend.cart import tax_cents, to_cents
from backend.settings import settings

DEBOUNCE_S = 1.2
LLM_TIMEOUT_S = 2.5
MAX_WORDS = 25

SYSTEM_PROMPT = (
    "You are the SpeedMart store agent inside a small smart shelf store.\n"
    "Write exactly one sentence of at most 20 words for the shopper's phone.\n"
    "Follow the DECISION exactly. Use only prices and product names given. No emojis, no hashtags, no quotes.\n"
    "If a GOAL is given, tie the sentence to it in the shopper's own words. Never invent a new goal.\n"
    "If the DECISION has caffeine true, say that the suggested product contains caffeine.\n"
    "Friendly, brief, practical. Use the shopper's first name at most once."
)

TEMPLATES = {
    "empty": "Cart's empty. Grab anything from the shelf.",
    "misplaced": "{name} is in the wrong bay. Please return it to bay {return_to_bay}.",
    "over_budget": "You're ${over_by} over budget. Putting back the {put_back_name} fixes it.",
    "suggest": "{cart_item} added. {sku_name} pairs well at ${price} and keeps you under budget.",
    "suggest_caffeine": "{cart_item} added. {sku_name} has caffeine, pairs well at ${price} and keeps you under budget.",
    "ok": "Looking good. ${remaining} left in your budget.",
}

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


# --- money helpers ---

def _usd(cents: int) -> float:
    return round(cents / 100, 2)


def fmt_usd(amount: float) -> str:
    """4.0 -> '4', 3.5 -> '3.50' (no $ sign; templates add it)."""
    cents = to_cents(amount)
    return str(cents // 100) if cents % 100 == 0 else f"{cents / 100:.2f}"


def _complements(sku: str) -> list[str]:
    """SKUs whose catalog tags say complements:<sku>, in catalog order."""
    return [s.sku for s in settings.skus.values() if f"complements:{sku}" in s.tags and s.sku != sku]


# --- 7.4 policy ---

def policy(cart: dict[str, Any], member: dict[str, Any] | None = None) -> dict[str, Any]:
    """First match wins: empty, misplaced, over_budget, suggest, ok. Pure function of the snapshot."""
    member = member or {}
    items = cart.get("items") or []
    budget_usd = cart.get("budget_usd", member.get("budget_usd", settings.store.get("default_budget_usd", 20)))
    budget = to_cents(budget_usd)
    total = to_cents(cart.get("total_usd", 0))

    if not items:
        return {"kind": "empty"}

    misplaced = [w for w in cart.get("warnings") or [] if w.get("kind") == "misplaced"]
    if misplaced:
        w = misplaced[0]
        name = w.get("name") or (settings.skus[w["sku"]].name if w.get("sku") in settings.skus else w.get("sku"))
        # return_to_bay is the number on the printed shelf card (bay id 0 is card 1), not the bay id.
        home = next((b.id for b in settings.bays if b.sku == w.get("sku")), None)
        return {"kind": "misplaced", "sku": w.get("sku"), "bay": w.get("bay"), "name": name,
                "return_to_bay": home + 1 if home is not None else "its own"}

    if total > budget:
        # most expensive SKU in the cart by unit price; ties go to catalog/cart order
        top = max(items, key=lambda i: to_cents(i["unit_price_usd"]))
        return {"kind": "over_budget", "put_back": top["sku"], "put_back_name": top["name"],
                "over_by": _usd(total - budget)}

    in_cart = {i["sku"] for i in items}
    subtotal = to_cents(cart.get("subtotal_usd", 0))
    for item in items:
        for comp in _complements(item["sku"]):
            if comp in in_cart:
                continue
            price = to_cents(settings.skus[comp].price_usd)
            new_sub = subtotal + price
            new_total = new_sub + tax_cents(new_sub, settings.store["tax_rate"])
            if new_total <= budget:
                decision = {"kind": "suggest", "sku": comp, "sku_name": settings.skus[comp].name,
                            "cart_item": item["name"], "price": _usd(price),
                            "remaining_after": _usd(budget - new_total)}
                if "caffeine" in settings.skus[comp].tags:  # the shopper hears about caffeine before they grab it
                    decision["caffeine"] = True
                return decision

    return {"kind": "ok", "remaining": _usd(budget - total)}


def template(decision: dict[str, Any]) -> str:
    d = dict(decision)
    for key in ("over_by", "price", "remaining", "remaining_after"):
        if key in d:
            d[key] = fmt_usd(d[key])
    kind = "suggest_caffeine" if decision["kind"] == "suggest" and decision.get("caffeine") else decision["kind"]
    return TEMPLATES[kind].format(**d)


def refund_line(amount_usd: float, card_last4: str | None) -> str:
    """The agent's line when a return completes. Fixed wording: a refund is money, so no LLM phrasing."""
    card = f"your Visa ending {card_last4}" if card_last4 else "your card"
    return f"Refund of ${to_cents(amount_usd) / 100:.2f} is on its way to {card}."


# --- LLM phrasing ---

def _first_name(member: dict[str, Any]) -> str:
    return (member.get("name") or "").strip().split(" ")[0]


def llm_available() -> bool:
    if not settings.features.llm:
        return False
    env = settings.env
    if env.llm_provider.lower() == "openai":
        return bool(env.openai_api_key and env.openai_model)
    return bool(env.anthropic_api_key)


def active_plan(member: dict[str, Any] | None) -> dict[str, Any] | None:
    """The F18 plan this shopper asked for, or None. Never raises: no plan just means no goal in the prompt."""
    member_id = (member or {}).get("id")
    if not member_id:
        return None
    try:
        from backend import intent

        return intent.current_plan(member_id)
    except Exception:
        return None


def _goal_for_prompt(plan: dict[str, Any] | None) -> dict[str, Any] | None:
    """Only the words, never the plan's money: a price the LLM was not given is rejected by validate()."""
    if not plan:
        return None
    summary = (plan.get("goal_summary") or "").strip()
    items = [i["name"] for i in plan.get("items") or [] if i.get("name")]
    if not summary and not items:
        return None
    return {"summary": summary, "items": items}


def _user_message(decision: dict[str, Any], cart: dict[str, Any], member: dict[str, Any],
                  plan: dict[str, Any] | None = None) -> str:
    payload = {
        "first_name": _first_name(member),
        "dietary": member.get("dietary"),
        "budget_usd": cart.get("budget_usd"),
        "cart": [{"name": i["name"], "qty": i["qty"], "unit_price_usd": i["unit_price_usd"]}
                 for i in cart.get("items") or []],
        "total_usd": cart.get("total_usd"),
        "decision": decision,
    }
    goal = _goal_for_prompt(plan)
    if goal:  # the key is absent entirely when there is no plan
        payload["goal"] = goal
    return json.dumps(payload)


def llm_phrase(decision: dict[str, Any], cart: dict[str, Any], member: dict[str, Any],
               plan: dict[str, Any] | None = None) -> str:
    """One raw LLM reply. Raises on any HTTP, timeout or shape problem; the caller falls back."""
    env = settings.env
    user = _user_message(decision, cart, member, plan)
    if env.llm_provider.lower() == "openai":
        r = httpx.post(
            f"{env.openai_base_url.rstrip('/')}/chat/completions",
            headers={"authorization": f"Bearer {env.openai_api_key}", "content-type": "application/json"},
            json={"model": env.openai_model, "max_tokens": 80,
                  "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]},
            timeout=LLM_TIMEOUT_S,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    r = httpx.post(
        ANTHROPIC_URL,
        headers={"x-api-key": env.anthropic_api_key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": env.anthropic_model, "max_tokens": 80, "system": SYSTEM_PROMPT,
              "messages": [{"role": "user", "content": user}]},
        timeout=LLM_TIMEOUT_S,
    )
    r.raise_for_status()
    return r.json()["content"][0]["text"]


_MONEY = re.compile(r"\$\s*(\d+(?:\.\d{1,2})?)")
_CAP_WORD = re.compile(r"[A-Z][A-Za-z'’]*")
_ALWAYS_OK = {"i", "ok", "usd"}


def _is_emoji(ch: str) -> bool:
    return unicodedata.category(ch) in ("So", "Cs", "Co") or ch in ("‍", "️", "︎") \
        or 0x1F000 <= ord(ch) <= 0x1FAFF


def _allowed_amounts(decision: dict[str, Any], cart: dict[str, Any]) -> set[int]:
    cents = {to_cents(s.price_usd) for s in settings.skus.values()}
    for key in ("over_by", "price", "remaining", "remaining_after"):
        if key in decision:
            cents.add(to_cents(decision[key]))
    for key in ("subtotal_usd", "tax_usd", "total_usd", "budget_usd"):
        if cart.get(key) is not None:
            cents.add(to_cents(cart[key]))
    for i in cart.get("items") or []:
        cents.add(to_cents(i["line_total_usd"]))
    return cents


def validate(line: Any, decision: dict[str, Any], cart: dict[str, Any], member: dict[str, Any],
             plan: dict[str, Any] | None = None) -> str | None:
    """The cleaned line if it may be shown, else None (caller keeps the template).

    Rules (9.6): one line, at most 25 words, no emoji, names only from the catalog. Also: every $ amount must be
    one the policy or cart gave it, so the LLM can never invent a price. A caffeine suggestion must say caffeine.
    """
    if not isinstance(line, str):
        return None
    text = line.strip().strip('"“”').strip()
    if not text or "\n" in text or "\r" in text:
        return None
    if len(text.split()) > MAX_WORDS:
        return None
    if any(_is_emoji(ch) for ch in text) or "#" in text:
        return None
    if decision.get("caffeine") and "caffeine" not in text.lower():
        return None

    allowed = {w.lower() for s in settings.skus.values() for w in re.findall(r"[A-Za-z']+", s.name)}
    allowed |= {w.lower() for w in re.findall(r"[A-Za-z']+", settings.store.get("name", ""))}
    allowed |= {"speedmart", "caffeine", *_ALWAYS_OK}
    first = _first_name(member).lower()
    if first:
        allowed.add(first)
    if plan:  # the shopper's own goal words ("Rehydrate after a run") are theirs to echo back
        allowed |= {w.lower() for w in re.findall(r"[A-Za-z']+", plan.get("goal_summary") or "")}
    for m in _CAP_WORD.finditer(text):
        before = text[:m.start()].rstrip()
        sentence_start = not before or before[-1] in ".!?:"
        if sentence_start:
            continue
        word = re.sub(r"['’]s$", "", m.group(0).lower())  # Maya's -> maya
        if word not in allowed and word.removesuffix("s") not in allowed:
            return None  # a proper noun we don't sell (e.g. another brand)

    amounts = _allowed_amounts(decision, cart)
    for m in _MONEY.finditer(text):
        if to_cents(m.group(1)) not in amounts:
            return None
    return text


def generate(cart: dict[str, Any], member: dict[str, Any] | None = None) -> tuple[str, str, dict[str, Any]]:
    """(line, source, decision). source is 'template' or 'llm'. Never raises."""
    member = member or {}
    decision = policy(cart, member)
    line = template(decision)
    source = "template"
    if llm_available():
        plan = active_plan(member)  # F18: let the line reference the shopper's goal when there is one
        try:
            raw = llm_phrase(decision, cart, member, plan)
        except Exception as e:  # timeout, HTTP error, bad JSON: the template stands
            eventlog.log("agent_llm_error", error=repr(e)[:300], kind=decision["kind"])
        else:
            ok = validate(raw, decision, cart, member, plan)
            if ok is not None:
                line, source = ok, "llm"
            else:
                eventlog.log("agent_llm_rejected", raw=str(raw)[:300], kind=decision["kind"])
    return line, source, decision


# --- debounce + delivery ---

def _cart_key(cart: dict[str, Any]) -> tuple:
    return (cart.get("session_id"), tuple((i["sku"], i["qty"]) for i in cart.get("items") or []),
            tuple((w.get("sku"), w.get("bay")) for w in cart.get("warnings") or []), cart.get("budget_usd"))


class Agent:
    """Keeps the latest line per session and a single debounced worker thread."""

    def __init__(self, debounce_s: float = DEBOUNCE_S) -> None:
        self.debounce_s = debounce_s
        self._cond = threading.Condition()
        self._pending: tuple[dict[str, Any], dict[str, Any], str | None] | None = None
        self._due = 0.0
        self._seq = 0  # bumps on every schedule; a result is dropped if a newer cart arrived meanwhile
        self._scheduled_key: tuple | None = None
        self._lines: dict[str, str] = {}
        self._thread: threading.Thread | None = None

    def line_for(self, session_id: str | None) -> str:
        with self._cond:
            return self._lines.get(session_id or "", "")

    def on_cart(self, snapshot: dict[str, Any], member: dict[str, Any] | None = None) -> dict[str, Any]:
        """Attach the stored line to the snapshot and schedule a new one if the cart content changed."""
        member = member or {}
        key = _cart_key(snapshot)
        with self._cond:
            snapshot["agent_line"] = self._lines.get(snapshot.get("session_id") or "", "")
            if key != self._scheduled_key:
                self._scheduled_key = key
                self._seq += 1
                self._pending = (dict(snapshot), dict(member), member.get("id"))
                self._due = time.monotonic() + self.debounce_s
                self._ensure_thread()
                self._cond.notify_all()
        return snapshot

    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="agent", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            with self._cond:
                while self._pending is None:
                    self._cond.wait()
                while (wait := self._due - time.monotonic()) > 0:
                    self._cond.wait(wait)  # a newer cart moves _due forward: latest cart wins
                cart, member, member_id = self._pending
                seq = self._seq
                self._pending = None
            try:
                self._deliver(cart, member, member_id, seq)
            except Exception as e:  # never let the agent thread die
                eventlog.log("agent_error", error=repr(e)[:300])

    def _deliver(self, cart: dict[str, Any], member: dict[str, Any], member_id: str | None, seq: int) -> None:
        line, source, decision = generate(cart, member)
        session_id = cart.get("session_id") or ""
        with self._cond:
            if seq != self._seq:
                return  # the cart changed while we were phrasing; the newer one is already scheduled
            # one shopper at a time: keep only the current session's line
            self._lines = {session_id: line}
        eventlog.log("agent_line", session_id=session_id, source=source, decision=decision, line=line)
        msg = {"type": "agent", "data": {"line": line}}
        if member_id:
            ws.manager.publish(msg, member_id=member_id, admin=True)
        else:
            ws.broadcast_admin(msg)


agent = Agent()


def on_cart(snapshot: dict[str, Any], member: dict[str, Any] | None = None) -> dict[str, Any]:
    """Hook called by store.py on every cart recompute. Returns the same snapshot with agent_line set."""
    return agent.on_cart(snapshot, member)
