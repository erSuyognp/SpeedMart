"""F18 "Tell the store what you need": shopper goal -> plan from the catalog, lit bays, cart vs plan at exit.

The LLM proposes a plan; this module validates it strictly and falls back to a keyword rules planner, so a
returned plan always uses real SKUs, real stock limits and fits the budget. All money math in integer cents.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend import db, eventlog
from backend.cart import tax_cents, to_cents, to_usd
from backend.settings import settings

LLM_TIMEOUT_S = 3.0
MAX_TEXT_CHARS = 300
MAX_REASON_WORDS = 12
MAX_SUMMARY_WORDS = 12
ENDED_STATES = ("PAID", "CLOSED", "CANCELLED")

router = APIRouter()

_lock = threading.Lock()
_plans: dict[str, dict[str, Any]] = {}  # member_id -> latest plan (in memory only)


class PlanInvalid(Exception):
    """The LLM reply broke a rule; the message says which one (logged as fallback_reason)."""


# --- catalog helpers ---

def units_available(sku: str) -> int:
    """Number of physical units of this SKU in catalog.json (the most a plan may ask for)."""
    return sum(1 for u in settings.units.values() if u.sku == sku)


def catalog_for_prompt() -> list[dict[str, Any]]:
    return [{"sku": s.sku, "name": s.name, "price_usd": to_usd(to_cents(s.price_usd)),
             "units_available": units_available(s.sku), "tags": s.tags}
            for s in settings.skus.values()]


def total_with_tax_cents(lines: dict[str, int]) -> int:
    subtotal = sum(to_cents(settings.skus[sku].price_usd) * qty for sku, qty in lines.items())
    return subtotal + tax_cents(subtotal, settings.store["tax_rate"])


def bays_for(skus: set[str]) -> list[int]:
    return [b.id for b in settings.bays if b.sku in skus]


def _money(cents: int) -> str:
    return f"${cents // 100}" if cents % 100 == 0 else f"${cents / 100:.2f}"


def _words(text: str) -> list[str]:
    return text.split()


# --- budget ---

_BUDGET_PATTERNS = [
    re.compile(r"(?:under|below|less than|max(?:imum)?|budget(?: of| is)?|up to|within|no more than|at most)"
               r"\s*\$?\s*(\d+(?:\.\d{1,2})?)"),
    re.compile(r"\$\s*(\d+(?:\.\d{1,2})?)"),
    re.compile(r"(\d+(?:\.\d{1,2})?)\s*(?:dollars?|bucks|usd)\b"),
]


def budget_from_text(text: str) -> int | None:
    """Lowest dollar amount the shopper states ("under $15", "15 bucks"), in cents, or None."""
    found = [to_cents(m) for p in _BUDGET_PATTERNS for m in p.findall(text.lower())]
    found = [c for c in found if c > 0]
    return min(found) if found else None


def effective_budget_cents(member_budget_usd: float, text: str, llm_override_usd: Any = None) -> int:
    """Member budget, lowered (never raised) by a budget stated in the text or read out of it by the LLM."""
    candidates = [to_cents(member_budget_usd)]
    stated = budget_from_text(text)
    if stated is not None:
        candidates.append(stated)
    if isinstance(llm_override_usd, (int, float)) and not isinstance(llm_override_usd, bool) and llm_override_usd > 0:
        candidates.append(to_cents(llm_override_usd))
    return min(candidates)


# --- LLM planner ---

SYSTEM_PROMPT = """You are the SpeedMart store planner inside a small smart shelf store.
The shopper tells you what they need. Build a short shopping plan using ONLY the catalog given.
Reply with JSON only, no prose, no code fences, exactly this shape:
{"goal_summary": str, "items": [{"sku": str, "qty": int, "reason": str}], "budget_override_usd": number or null}
Rules:
- goal_summary: a short lowercase phrase of at most 6 words, e.g. "run recovery" or "vegan snacks for two".
- sku must be one of the catalog skus. qty from 1 to units_available.
- reason: at most 10 words, why this item fits the goal.
- The total of price_usd * qty plus tax (tax_rate) must not exceed the budget.
- budget_override_usd: the budget the shopper states in their text (e.g. "under $15" -> 15), else null.
- Respect the dietary tag when it is set. Pick only items that fit the goal; fewer is fine."""


def _llm_config() -> tuple[str, str, str] | None:
    """(provider, key, model) when the LLM is on and configured, else None."""
    if not settings.features.llm:
        return None
    env = settings.env
    provider = env.llm_provider.split("#")[0].strip().lower()
    if provider == "anthropic" and env.anthropic_api_key and env.anthropic_model:
        return "anthropic", env.anthropic_api_key, env.anthropic_model
    if provider == "openai" and env.openai_api_key and env.openai_model:
        return "openai", env.openai_api_key, env.openai_model
    return None


def call_llm(text: str, member: dict[str, Any]) -> str:
    """Raw reply text from the configured provider. Raises on any HTTP or shape problem."""
    cfg = _llm_config()
    if cfg is None:
        raise RuntimeError("llm_not_configured")
    provider, key, model = cfg
    user = json.dumps({
        "catalog": catalog_for_prompt(),
        "budget_usd": member["budget_usd"],
        "tax_rate": settings.store["tax_rate"],
        "dietary": member.get("dietary") or None,
        "text": text,
    })
    if provider == "anthropic":
        r = httpx.post("https://api.anthropic.com/v1/messages",
                       headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                "content-type": "application/json"},
                       json={"model": model, "max_tokens": 400, "system": SYSTEM_PROMPT,
                             "messages": [{"role": "user", "content": user}]},
                       timeout=LLM_TIMEOUT_S)
        r.raise_for_status()
        return r.json()["content"][0]["text"]
    r = httpx.post(f"{settings.env.openai_base_url.rstrip('/')}/chat/completions",
                   headers={"Authorization": f"Bearer {key}", "content-type": "application/json"},
                   json={"model": model, "max_tokens": 400, "temperature": 0.2,
                         "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                      {"role": "user", "content": user}]},
                   timeout=LLM_TIMEOUT_S)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def strip_code_fences(raw: str) -> str:
    s = raw.strip()
    m = re.match(r"^```[a-zA-Z0-9_-]*\s*\n?(.*?)\n?```$", s, re.DOTALL)
    return m.group(1).strip() if m else s


def parse_llm_reply(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(strip_code_fences(raw))
    except (json.JSONDecodeError, TypeError) as e:
        raise PlanInvalid(f"not_json: {e}") from e
    if not isinstance(data, dict):
        raise PlanInvalid("not_an_object")
    return data


def validate_llm_plan(data: dict[str, Any], text: str, member: dict[str, Any]) -> tuple[str, dict[str, int],
                                                                                        dict[str, str], int]:
    """Strict checks. Returns (goal_summary, {sku: qty}, {sku: reason}, budget_cents) or raises PlanInvalid."""
    summary = data.get("goal_summary")
    if not isinstance(summary, str) or not summary.strip():
        raise PlanInvalid("goal_summary_missing")
    summary = " ".join(summary.split())
    if len(_words(summary)) > MAX_SUMMARY_WORDS:
        raise PlanInvalid("goal_summary_too_long")

    override = data.get("budget_override_usd")
    if override is not None and (isinstance(override, bool) or not isinstance(override, (int, float))):
        raise PlanInvalid("budget_override_not_a_number")
    budget = effective_budget_cents(member["budget_usd"], text, override)

    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise PlanInvalid("items_missing")
    lines: dict[str, int] = {}
    reasons: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            raise PlanInvalid("item_not_an_object")
        sku, qty, reason = item.get("sku"), item.get("qty"), item.get("reason")
        if sku not in settings.skus:
            raise PlanInvalid(f"unknown_sku:{sku}")
        if sku in lines:
            raise PlanInvalid(f"duplicate_sku:{sku}")
        if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= units_available(sku):
            raise PlanInvalid(f"bad_qty:{sku}:{qty}")
        if not isinstance(reason, str) or not reason.strip():
            raise PlanInvalid(f"reason_missing:{sku}")
        reason = " ".join(reason.split())
        if len(_words(reason)) > MAX_REASON_WORDS:
            raise PlanInvalid(f"reason_too_long:{sku}")
        lines[sku] = qty
        reasons[sku] = reason
    if total_with_tax_cents(lines) > budget:
        raise PlanInvalid("over_budget")
    return summary, lines, reasons, budget


# --- rules planner (fallback, and the only planner when the LLM is off) ---

TAG_WORDS: dict[str, list[str]] = {
    "recovery": ["recovery", "recover", "run", "running", "ran", "jog", "jogging", "workout", "gym", "exercise",
                 "sweat", "marathon", "training", "hydrate", "hydration", "electrolyte", "cramp"],
    "drink": ["drink", "thirsty", "beverage", "sip", "hydrate", "hydration", "refresh"],
    "snack": ["snack", "hungry", "protein", "bite", "munch", "food", "energy"],
}
TAG_REASONS = {
    "recovery": "Replaces electrolytes after a workout",
    "drink": "Something to drink",
    "snack": "Quick protein snack",
}
_COUNT_WORDS = {"two": 2, "2": 2, "three": 3, "3": 3, "four": 4, "4": 4, "couple": 2, "pair": 2}


def _word_matches(token: str, word: str) -> bool:
    """Loose match for plurals and verb forms: 'drinks'~'drink', 'recovering'~'recovery'."""
    word = word.lower()
    if len(word) <= 4:
        return token in (word, word + "s")
    return token.startswith(word[:max(4, len(word) - 2)])


def _qty_wanted(tokens: list[str]) -> int:
    for i, t in enumerate(tokens):
        if t in ("for", "x") and i + 1 < len(tokens) and tokens[i + 1] in _COUNT_WORDS:
            return _COUNT_WORDS[tokens[i + 1]]
        if t in ("couple", "pair"):
            return 2
    return 1


def _short_goal(text: str) -> str:
    """The shopper's words minus any budget phrase, at most 6 words: "post run recovery"."""
    t = text.lower()
    for p in _BUDGET_PATTERNS:
        t = p.sub(" ", t)
    words = re.sub(r"[^\w\s']", " ", t).split()
    return " ".join(words[:6]) or "your visit"


def rules_plan(text: str, member: dict[str, Any]) -> tuple[str, dict[str, int], dict[str, str], int]:
    """Match words against SKU names and tags, add complements, fill greedily within the budget."""
    budget = effective_budget_cents(member["budget_usd"], text)
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    scores: dict[str, int] = {}
    reasons: dict[str, str] = {}
    tag_matched: set[str] = set()
    for sku, s in settings.skus.items():
        score = 0
        for tag in s.tags:
            if tag.startswith("complements:"):
                continue
            vocab = TAG_WORDS.get(tag, []) + [tag]
            if any(_word_matches(t, w) for t in tokens for w in vocab):
                score += 2
                tag_matched.add(sku)
                reasons.setdefault(sku, TAG_REASONS.get(tag, f"Good for {tag}"))
        name_hits = [w for w in re.findall(r"[a-z]+", s.name.lower()) if any(_word_matches(t, w) for t in tokens)]
        if name_hits:
            score += len(name_hits)
            reasons.setdefault(sku, f"Matches {name_hits[0]}")
        if score:
            scores[sku] = score
    # complements of matched items ("complements:elx" on rec means rec pairs with elx)
    for sku, s in settings.skus.items():
        for tag in s.tags:
            base = tag.split(":", 1)[1] if tag.startswith("complements:") else None
            if base in tag_matched and sku not in tag_matched:
                scores[sku] = scores.get(sku, 0) + 1
                reasons[sku] = f"Pairs well with {settings.skus[base].name}"

    order = list(settings.skus)
    wanted = _qty_wanted(tokens)
    lines: dict[str, int] = {}
    for sku in sorted(scores, key=lambda k: (-scores[k], order.index(k))):
        for qty in range(min(wanted, units_available(sku)), 0, -1):
            if total_with_tax_cents({**lines, sku: qty}) <= budget:
                lines[sku] = qty
                break
    reasons = {sku: " ".join(_words(reasons[sku])[:MAX_REASON_WORDS]) for sku in lines}
    return _short_goal(text), lines, reasons, budget


# --- plan assembly ---

def build_plan(summary: str, lines: dict[str, int], reasons: dict[str, str], budget: int, source: str) -> dict:
    items = []
    for sku, s in settings.skus.items():  # catalog order
        if sku in lines:
            items.append({"sku": sku, "name": s.name, "qty": lines[sku],
                          "unit_price_usd": to_usd(to_cents(s.price_usd)), "reason": reasons[sku]})
    total = total_with_tax_cents(lines)
    return {
        "plan_id": f"pln_{secrets.token_urlsafe(8)}",
        "goal_summary": summary,
        "items": items,
        "est_total_usd": to_usd(total),
        "budget_usd": to_usd(budget),
        "fits_budget": total <= budget,
        "bays": bays_for(set(lines)),
        "source": source,
        "created_at": db.now_iso(),
    }


def make_plan(text: str, member: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """(plan, fallback_reason). Tries the LLM, validates, else rules. Never returns an invalid plan."""
    fallback_reason = None
    if _llm_config() is not None:
        try:
            data = parse_llm_reply(call_llm(text, member))
            return build_plan(*validate_llm_plan(data, text, member), source="llm"), None
        except PlanInvalid as e:
            fallback_reason = f"invalid:{e}"
        except Exception as e:  # timeout, HTTP error, odd response shape
            fallback_reason = f"llm_error:{type(e).__name__}"
    else:
        fallback_reason = "llm_off"
    return build_plan(*rules_plan(text, member), source="rules"), fallback_reason


# --- shelf highlights (F12). serial_bridge.highlight / clear_highlights may not exist yet: skip if missing. ---

def _serial(name: str, *args: Any) -> bool:
    if not settings.features.hardware_leds:
        return False
    try:
        from backend import serial_bridge
        fn = getattr(serial_bridge, name, None)
        if not callable(fn):
            return False
        fn(*args)
        return True
    except Exception as e:  # LEDs are decoration; never break planning
        eventlog.log("intent_highlight_error", helper=name, error=repr(e))
        return False


def _someone_else_inside(member_id: str) -> bool:
    try:
        from backend import store
        session = store.current_session()
    except Exception:
        return False
    return session is not None and session["member_id"] != member_id


def show_highlights(plan: dict[str, Any], member_id: str) -> None:
    if _someone_else_inside(member_id):
        return  # never blink bays for a shopper who is not the one in the store
    _serial("clear_highlights")
    for bay in plan["bays"]:
        _serial("highlight", bay, True)


def clear_highlights() -> None:
    _serial("clear_highlights")


def _on_event(entry: dict[str, Any]) -> None:
    """eventlog subscriber: session ended -> lights off; fresh entry -> forget the previous visit's plan."""
    if entry.get("type") != "session_state":
        return
    if entry.get("to") in ENDED_STATES:
        clear_highlights()
    elif entry.get("to") == "IN_STORE" and entry.get("from") is None and entry.get("member_id"):
        with _lock:
            _plans.pop(entry["member_id"], None)


_subscribed = False


def _subscribe_once() -> None:
    global _subscribed
    if not _subscribed:
        eventlog.subscribe(_on_event)
        _subscribed = True


_subscribe_once()


# --- cart vs plan (shown on the exit screen) ---

def _join(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def compare_cart_to_plan(cart_snapshot: dict[str, Any] | None, plan: dict[str, Any] | None) -> dict[str, Any]:
    """Pure function. missing/extra list {"sku","name","qty"} where qty is the shortfall or the surplus."""
    if not plan:
        return {"matches": False, "missing": [], "extra": [], "summary": "No shopping plan for this visit."}
    cart_items = (cart_snapshot or {}).get("items") or []
    have = {i["sku"]: int(i["qty"]) for i in cart_items}
    want = {i["sku"]: int(i["qty"]) for i in plan.get("items") or []}
    names = {i["sku"]: i.get("name", i["sku"]) for i in [*(plan.get("items") or []), *cart_items]}

    missing = [{"sku": s, "name": names[s], "qty": q - have.get(s, 0)} for s, q in want.items() if have.get(s, 0) < q]
    extra = [{"sku": s, "name": names[s], "qty": q - want.get(s, 0)} for s, q in have.items() if q > want.get(s, 0)]
    matches = not missing and not extra

    n = len(want)
    if not cart_items:
        status = "you haven't picked anything up yet" if want else "your cart is empty"
    elif matches:
        status = "you have it" if n == 1 else "you have both items" if n == 2 else f"you have all {n} items"
    elif missing and extra:
        status = f"you're missing {_join([m['name'] for m in missing])} and also picked {_join([e['name'] for e in extra])}"
    elif missing:
        status = f"you still need {_join([m['name'] for m in missing])}"
    else:
        status = f"you have everything, plus {_join([e['name'] for e in extra])}"

    budget = to_cents(plan.get("budget_usd", 0))
    total = to_cents((cart_snapshot or {}).get("total_usd", 0))
    money = f", {_money(total)}" if cart_items else ""
    over = f", over your {_money(budget)} budget" if cart_items and budget and total > budget else ""
    goal = plan.get("goal_summary") or "your list"
    budget_part = f" under {_money(budget)}" if budget and "$" not in goal else ""
    summary = f"You asked for {goal}{budget_part}: {status}{money}{over}."
    return {"matches": matches, "missing": missing, "extra": extra, "summary": summary}


# --- routes ---

class IntentIn(BaseModel):
    text: str


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, "message": message})


def _load_member(member_id: str) -> dict[str, Any] | None:
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def current_plan(member_id: str) -> dict[str, Any] | None:
    """Latest plan for this member (used by the exit screen integration)."""
    with _lock:
        return _plans.get(member_id)


@router.post("/api/intent")
def post_intent(body: IntentIn, request: Request):
    member_id = request.session.get("member_id")
    if not member_id:
        return _error(401, "not_logged_in", "Please sign in first.")
    text = " ".join(body.text.split())
    if not text:
        return _error(400, "empty_text", "Tell us what you need, for example \"a quick protein snack\".")
    if len(text) > MAX_TEXT_CHARS:
        return _error(400, "text_too_long", f"Please keep it under {MAX_TEXT_CHARS} characters.")
    member = _load_member(member_id)
    if member is None:
        return _error(404, "unknown_member", "We couldn't find your membership. Please sign in again.")

    plan, fallback_reason = make_plan(text, member)
    with _lock:
        _plans[member_id] = plan
    eventlog.log("intent_plan", member_id=member_id, text=text, plan_id=plan["plan_id"], source=plan["source"],
                 fallback_reason=fallback_reason, items={i["sku"]: i["qty"] for i in plan["items"]},
                 est_total_usd=plan["est_total_usd"], budget_usd=plan["budget_usd"], bays=plan["bays"])
    show_highlights(plan, member_id)
    return plan


@router.get("/api/intent/current")
def get_current(request: Request):
    member_id = request.session.get("member_id")
    if not member_id:
        return _error(401, "not_logged_in", "Please sign in first.")
    return current_plan(member_id)


@router.delete("/api/intent")
def delete_intent(request: Request):
    member_id = request.session.get("member_id")
    if not member_id:
        return _error(401, "not_logged_in", "Please sign in first.")
    with _lock:
        plan = _plans.pop(member_id, None)
    if plan is not None:
        eventlog.log("intent_cleared", member_id=member_id, plan_id=plan["plan_id"])
        if not _someone_else_inside(member_id):
            clear_highlights()
    return {"ok": True}
