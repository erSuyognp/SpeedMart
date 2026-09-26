"""S4.3 tests: AI store agent. Policy branches, templates, LLM validation and fallback, debounce. No network:
httpx.post is replaced in every test and fails the test if anything reaches it unexpectedly."""

from __future__ import annotations

import dataclasses
import json
import time

from pathlib import Path

import httpx
import pytest

from backend import agent, cart, db, eventlog, shelf_state, store, ws
from backend.settings import settings

BUDGET = 20
ROOT = Path(__file__).resolve().parent.parent
CATALOG = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def _full_shelf() -> dict[int, list[int]]:
    """Every unit in its home bay, one entry per bay in config.json."""
    bays: dict[int, list[int]] = {int(b["id"]): [] for b in CONFIG["bays"]}
    for unit in sorted(CATALOG["units"], key=lambda u: u["tag_id"]):
        bays.setdefault(int(unit["home_bay"]), []).append(int(unit["tag_id"]))
    return bays


FULL = _full_shelf()
BASELINE = {sku: sum(u["sku"] == sku for u in CATALOG["units"]) for sku in (s["sku"] for s in CATALOG["skus"])}
MEMBER = {"id": "mem_test", "name": "Maya Lin", "budget_usd": BUDGET, "dietary": None}


def make_cart(picked: dict[str, int], budget: float = BUDGET, misplaced: list | None = None,
              session_id: str = "ses_test") -> dict:
    session = {"id": session_id, "state": "IN_STORE", "baseline": BASELINE}
    shelf = {sku: n - picked.get(sku, 0) for sku, n in BASELINE.items()}
    return cart.compute_cart(session, shelf, {}, {**MEMBER, "budget_usd": budget}, misplaced=misplaced)


def misplaced_bar() -> list[dict]:
    return [{"tag_id": 4, "sku": "bar", "name": "Chips", "bay": 0}]


OVER = {"mix": 2, "elx": 2, "rec": 2}  # 21.00 + 1.68 tax = 22.68 on a $20 budget
CAFFEINE_LINE = "Chips added. Energy drink has caffeine, pairs well at $3 and keeps you under budget."


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad status", request=httpx.Request("POST", "http://x"),
                                        response=httpx.Response(self.status_code))

    def json(self):
        return self._payload


def anthropic_reply(text: str) -> FakeResponse:
    return FakeResponse({"content": [{"type": "text", "text": text}]})


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any httpx.post not replaced by the test itself is a failure."""
    stray = []

    def blocked(*args, **kwargs):
        stray.append((args, kwargs))
        raise RuntimeError("network blocked in tests")

    monkeypatch.setattr(agent.httpx, "post", blocked)
    yield
    assert not stray, f"unexpected network call: {stray}"


def use_settings(monkeypatch, *, llm: bool = True, provider: str = "anthropic", key: str = "test-key",
                 openai_model: str = "test-model"):
    env = dataclasses.replace(settings.env, llm_provider=provider, anthropic_api_key=key if provider == "anthropic"
                              else "", openai_api_key=key if provider == "openai" else "",
                              openai_model=openai_model, openai_base_url="https://llm.example/v1")
    features = dataclasses.replace(settings.features, llm=llm)
    monkeypatch.setattr(agent, "settings", dataclasses.replace(settings, env=env, features=features))


def fake_llm(monkeypatch, reply):
    calls = []

    def post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(agent.httpx, "post", post)
    return calls


# --- 7.4 policy: every branch, first match wins ---

def test_policy_empty():
    assert agent.policy(make_cart({}), MEMBER) == {"kind": "empty"}


def test_policy_empty_beats_misplaced():
    assert agent.policy(make_cart({}, misplaced=misplaced_bar()), MEMBER)["kind"] == "empty"


def test_policy_misplaced():
    d = agent.policy(make_cart({"elx": 1}, misplaced=misplaced_bar()), MEMBER)
    assert d == {"kind": "misplaced", "sku": "bar", "bay": 0, "name": "Chips", "return_to_bay": 3}


def test_policy_misplaced_beats_over_budget():
    c = make_cart(OVER, misplaced=misplaced_bar())
    assert c["over_budget"]
    assert agent.policy(c, MEMBER)["kind"] == "misplaced"


def test_policy_over_budget_puts_back_most_expensive():
    c = make_cart(OVER)
    d = agent.policy(c, MEMBER)
    assert d == {"kind": "over_budget", "put_back": "mix", "put_back_name": "Vegan snack", "over_by": 2.68}


def test_policy_over_budget_on_the_demo_budget():
    # docs/DEMO.md: hydration drink + energy drink + vegan snack is 10.50 + 0.84 tax = 11.34 on $10
    d = agent.policy(make_cart({"elx": 1, "rec": 1, "mix": 1}, budget=10), MEMBER)
    assert d == {"kind": "over_budget", "put_back": "mix", "put_back_name": "Vegan snack", "over_by": 1.34}


def test_policy_suggests_complement_within_budget():
    d = agent.policy(make_cart({"wat": 1}), MEMBER)  # 1.62 now, 5.40 with the hydration drink
    assert d == {"kind": "suggest", "sku": "elx", "sku_name": "Hydration drink", "cart_item": "Water",
                 "price": 3.5, "remaining_after": 14.6}


def test_policy_flags_caffeine_when_suggesting_the_energy_drink():
    d = agent.policy(make_cart({"bar": 1}), MEMBER)  # 2.70 now, 5.94 with the energy drink
    assert d == {"kind": "suggest", "sku": "rec", "sku_name": "Energy drink", "cart_item": "Chips",
                 "price": 3.0, "remaining_after": 14.06, "caffeine": True}


def test_policy_no_suggestion_when_complement_breaks_budget():
    # 5.40 in the cart. The only complement of chips is the energy drink at 3.24 with tax, so an
    # 8.00 budget leaves no room for it.
    d = agent.policy(make_cart({"bar": 2}, budget=8), MEMBER)
    assert d == {"kind": "ok", "remaining": 2.6}


def test_policy_no_suggestion_when_complement_already_in_cart():
    # elx is a complement of wat and already in the cart, so it is skipped and the other one (mix) is offered.
    d = agent.policy(make_cart({"wat": 1, "elx": 1}), MEMBER)  # 5.40 with tax
    assert d["kind"] == "suggest" and d["sku"] == "mix"
    # with every complement that fits the budget already in the cart there is nothing left to suggest
    assert agent.policy(make_cart({"wat": 1, "elx": 1, "mix": 1}), MEMBER) == {"kind": "ok", "remaining": 10.28}


def test_policy_ok_without_complements():
    assert agent.policy(make_cart({"elx": 1}), MEMBER) == {"kind": "ok", "remaining": 16.22}


def test_policy_uses_member_budget():
    assert agent.policy(make_cart({"mix": 2}, budget=10), MEMBER)["kind"] == "ok"  # 8.64, 10.26 with water > 10
    assert agent.policy(make_cart({"mix": 2, "bar": 1}, budget=10), MEMBER)["kind"] == "over_budget"  # 11.34


# --- templates ---

@pytest.mark.parametrize("picked, misplaced, expected", [
    ({}, None, "Cart's empty. Grab anything from the shelf."),
    ({"elx": 1}, misplaced_bar(), "Chips is in the wrong bay. Please return it to bay 3."),
    (OVER, None, "You're $2.68 over budget. Putting back the Vegan snack fixes it."),
    ({"wat": 1}, None, "Water added. Hydration drink pairs well at $3.50 and keeps you under budget."),
    ({"bar": 1}, None, CAFFEINE_LINE),
    ({"elx": 1}, None, "Looking good. $16.22 left in your budget."),
])
def test_templates(picked, misplaced, expected):
    assert agent.template(agent.policy(make_cart(picked, misplaced=misplaced), MEMBER)) == expected


# --- LLM validation ---

GOOD = "Maya, Energy drink has caffeine and pairs nicely with your Chips for $3, keeping you under budget."


@pytest.mark.parametrize("raw", [
    "Line one.\nLine two.",
    " ".join(["word"] * 26),
    "Great pick \U0001F389 grab an Energy drink with caffeine for $3.",
    "Grab an Energy drink with caffeine for $3 ☕",
    "Try a Red Bull with your Chips, it has caffeine and keeps you under budget.",
    "An Energy drink has caffeine, only $2.75, and keeps you under budget.",
    "Add a #energy drink with caffeine for $3.",
    "Maya, Energy drink pairs nicely with your Chips for $3 and keeps you under budget.",  # no caffeine
    "",
    None,
    42,
])
def test_validate_rejects_bad_llm_output(raw):
    c = make_cart({"bar": 1})
    assert agent.validate(raw, agent.policy(c, MEMBER), c, MEMBER) is None


def test_validate_accepts_good_line():
    c = make_cart({"bar": 1})
    assert agent.validate(f'  "{GOOD}"  ', agent.policy(c, MEMBER), c, MEMBER) == GOOD


def test_validate_asks_for_caffeine_only_when_suggesting_it():
    c = make_cart({"wat": 1})  # suggests the hydration drink, which has no caffeine
    line = "Maya, a Hydration drink pairs well with your Water at $3.50."
    assert agent.validate(line, agent.policy(c, MEMBER), c, MEMBER) == line


def test_validate_allows_exactly_25_words():
    c = make_cart({"elx": 1})
    line = " ".join(["good"] * 25)
    assert agent.validate(line, agent.policy(c, MEMBER), c, MEMBER) == line


# --- generate(): LLM phrasing with template fallback ---

def test_llm_line_used_when_valid(monkeypatch):
    use_settings(monkeypatch)
    calls = fake_llm(monkeypatch, anthropic_reply(GOOD))
    line, source, decision = agent.generate(make_cart({"bar": 1}), MEMBER)
    assert (line, source, decision["kind"]) == (GOOD, "llm", "suggest")
    call = calls[0]
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["timeout"] == 2.5
    assert call["headers"]["x-api-key"] == "test-key"
    assert call["headers"]["anthropic-version"] == "2023-06-01"
    assert call["json"]["max_tokens"] == 80
    assert call["json"]["system"] == agent.SYSTEM_PROMPT
    user = json.loads(call["json"]["messages"][0]["content"])
    assert user["first_name"] == "Maya"
    assert user["decision"]["kind"] == "suggest"
    assert set(user) == {"first_name", "dietary", "budget_usd", "cart", "total_usd", "decision"}


def test_invalid_llm_output_falls_back_to_template(monkeypatch):
    use_settings(monkeypatch)
    fake_llm(monkeypatch, anthropic_reply("Buy a Gatorade instead \U0001F4AA"))
    line, source, _ = agent.generate(make_cart({"bar": 1}), MEMBER)
    assert source == "template"
    assert line == CAFFEINE_LINE


def test_timeout_falls_back_to_template(monkeypatch):
    use_settings(monkeypatch)
    calls = fake_llm(monkeypatch, httpx.ReadTimeout("timed out"))
    line, source, _ = agent.generate(make_cart({"elx": 1}), MEMBER)
    assert (line, source) == ("Looking good. $16.22 left in your budget.", "template")
    assert calls[0]["timeout"] == agent.LLM_TIMEOUT_S == 2.5


def test_http_error_and_bad_shape_fall_back(monkeypatch):
    use_settings(monkeypatch)
    fake_llm(monkeypatch, FakeResponse({}, status=529))
    assert agent.generate(make_cart({}), MEMBER)[1] == "template"
    fake_llm(monkeypatch, FakeResponse({"unexpected": True}))
    assert agent.generate(make_cart({}), MEMBER)[1] == "template"


def test_openai_compatible_provider(monkeypatch):
    use_settings(monkeypatch, provider="openai")
    reply = FakeResponse({"choices": [{"message": {"content": "Looking good, Maya, $16.22 left to spend."}}]})
    calls = fake_llm(monkeypatch, reply)
    line, source, _ = agent.generate(make_cart({"elx": 1}), MEMBER)
    assert (line, source) == ("Looking good, Maya, $16.22 left to spend.", "llm")
    assert calls[0]["url"] == "https://llm.example/v1/chat/completions"
    assert calls[0]["headers"]["authorization"] == "Bearer test-key"
    assert calls[0]["timeout"] == 2.5
    body = calls[0]["json"]
    assert body["model"] == "test-model" and body["max_tokens"] == 80
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


@pytest.mark.parametrize("kwargs", [
    {"llm": False},                                   # flag off, key present
    {"key": ""},                                      # flag on, no key
    {"provider": "openai", "openai_model": ""},       # openai without a model
])
def test_templates_only_without_llm(monkeypatch, kwargs):
    use_settings(monkeypatch, **kwargs)
    assert not agent.llm_available()
    line, source, _ = agent.generate(make_cart({"bar": 1}), MEMBER)  # no_network fails the test on any call
    assert source == "template" and line == CAFFEINE_LINE


# --- debounce, latest cart wins, delivery ---

@pytest.fixture
def published(monkeypatch):
    msgs = []
    # admin sockets also get every event-log line; keep only the agent messages
    monkeypatch.setattr(ws.manager, "publish",
                        lambda msg, **kw: msgs.append((msg, kw)) if msg["type"] == "agent" else None)
    return msgs


def wait_for(cond, timeout: float = 3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_debounce_latest_cart_wins(monkeypatch, published):
    use_settings(monkeypatch, llm=False)
    a = agent.Agent(debounce_s=0.2)
    a.on_cart(make_cart({}), MEMBER)
    a.on_cart(make_cart(OVER), MEMBER)
    a.on_cart(make_cart({"elx": 1}), MEMBER)
    assert wait_for(lambda: published)
    time.sleep(0.4)  # nothing else may arrive for the superseded carts
    assert [m["data"]["line"] for m, _ in published] == ["Looking good. $16.22 left in your budget."]
    msg, kw = published[0]
    assert msg["type"] == "agent" and kw == {"member_id": "mem_test", "admin": True}


def test_debounce_waits_before_generating(monkeypatch, published):
    use_settings(monkeypatch, llm=False)
    a = agent.Agent(debounce_s=0.5)
    a.on_cart(make_cart({}), MEMBER)
    time.sleep(0.2)
    assert not published
    assert wait_for(lambda: published)


def test_line_stored_on_next_snapshot(monkeypatch, published):
    use_settings(monkeypatch, llm=False)
    a = agent.Agent(debounce_s=0.01)
    first = a.on_cart(make_cart({"bar": 1}), MEMBER)
    assert first["agent_line"] == ""  # nothing generated yet; on_cart never blocks
    assert wait_for(lambda: published)
    again = a.on_cart(make_cart({"bar": 1}), MEMBER)
    assert again["agent_line"] == CAFFEINE_LINE
    time.sleep(0.1)
    assert len(published) == 1  # identical cart: not regenerated


def test_unchanged_cart_is_not_rescheduled(monkeypatch, published):
    use_settings(monkeypatch, llm=False)
    a = agent.Agent(debounce_s=0.01)
    for _ in range(5):
        a.on_cart(make_cart({"bar": 1}), MEMBER)
    assert wait_for(lambda: published)
    time.sleep(0.1)
    assert len(published) == 1


def test_stale_llm_result_is_dropped(monkeypatch, published):
    """A cart change while the LLM is phrasing wins over the older, slower result."""
    use_settings(monkeypatch)
    a = agent.Agent(debounce_s=0.01)
    started = []

    def slow_post(url, **kwargs):
        started.append(1)
        if len(started) == 1:
            a.on_cart(make_cart({"elx": 1}), MEMBER)  # the shopper changes the cart mid-call
        return anthropic_reply("Looking good, $16.22 left.")

    monkeypatch.setattr(agent.httpx, "post", slow_post)
    a.on_cart(make_cart({"bar": 1}), MEMBER)
    assert wait_for(lambda: published)
    time.sleep(0.1)
    assert [m["data"]["line"] for m, _ in published] == ["Looking good, $16.22 left."]
    assert len(started) == 2


def test_on_cart_never_calls_llm_on_caller_thread(monkeypatch, published):
    use_settings(monkeypatch)
    fake_llm(monkeypatch, httpx.ReadTimeout("slow"))
    a = agent.Agent(debounce_s=0.01)
    t0 = time.monotonic()
    a.on_cart(make_cart({"bar": 1}), MEMBER)
    assert time.monotonic() - t0 < 0.05
    assert wait_for(lambda: published)
    assert published[0][0]["data"]["line"] == CAFFEINE_LINE


# --- wired into the store: pick chips, the snapshot carries the suggestion ---

@pytest.fixture
def store_env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "speedmart.db")
    monkeypatch.setattr(eventlog, "DATA_DIR", tmp_path)
    monkeypatch.setattr(eventlog, "EVENTS_PATH", tmp_path / "events.log.jsonl")
    monkeypatch.setattr(agent, "agent", agent.Agent(debounce_s=0.01))
    shelf_state.reset()
    store._overrides.clear()
    db.init_db()
    db.seed_demo_member()
    yield tmp_path
    store.cancel(reason="test")
    shelf_state.reset()
    store._overrides.clear()


def snap(bays: dict[int, list[int]], frame_id: int) -> dict:
    return {"ts": frame_id, "frame_id": frame_id, "loose_units": [],
            "bays": [{"bay": b, "stable": True, "motion": False, "units": u, "yolo_counts": {}} for b, u in bays.items()]}


def test_store_pick_shows_suggestion(monkeypatch, store_env, published):
    use_settings(monkeypatch, llm=False)
    conn = db.connect()
    try:
        member_id = conn.execute("SELECT id FROM members WHERE is_demo = 1").fetchone()["id"]
    finally:
        conn.close()
    shelf_state.apply_snapshot(snap(FULL, 1))
    store.start_session(member_id)
    shelf_state.apply_snapshot(snap({**FULL, 2: [5]}, 2))  # chips picked (tag 4)
    store.on_shelf_change()
    expected = CAFFEINE_LINE
    assert wait_for(lambda: any(m["type"] == "agent" and m["data"]["line"] == expected for m, _ in published))
    assert store.current_cart()["agent_line"] == expected
    lines = [json.loads(x) for x in eventlog.EVENTS_PATH.read_text(encoding="utf-8").splitlines()]
    assert any(e["type"] == "agent_line" and e["decision"]["kind"] == "suggest" for e in lines)


# --- F18: the active intent plan reaches the agent line -------------------------------------------

PLAN = {
    "plan_id": "pln_test",
    "goal_summary": "study session fuel",
    "items": [{"sku": "rec", "name": "Energy drink", "qty": 1},
              {"sku": "bar", "name": "Chips", "qty": 1}],
    "est_total_usd": 5.94,
    "budget_usd": 10.0,
}


@pytest.fixture
def with_plan(monkeypatch):
    """intent.current_plan() answers with PLAN for the test member and nothing for anyone else."""
    from backend import intent

    monkeypatch.setattr(intent, "current_plan", lambda member_id: PLAN if member_id == MEMBER["id"] else None)
    return PLAN


def test_goal_is_sent_to_the_llm_when_a_plan_exists(monkeypatch, with_plan):
    use_settings(monkeypatch)
    calls = fake_llm(monkeypatch, anthropic_reply(GOOD))
    agent.generate(make_cart({"bar": 1}), MEMBER)
    user = json.loads(calls[0]["json"]["messages"][0]["content"])
    assert user["goal"] == {"summary": "study session fuel", "items": ["Energy drink", "Chips"]}
    assert user["decision"]["caffeine"] is True
    # The plan's own money never goes in the prompt: validate() would reject any amount it did not supply.
    assert "5.94" not in calls[0]["json"]["messages"][0]["content"]


def test_no_goal_key_without_a_plan(monkeypatch):
    use_settings(monkeypatch)
    calls = fake_llm(monkeypatch, anthropic_reply(GOOD))
    agent.generate(make_cart({"bar": 1}), MEMBER)
    user = json.loads(calls[0]["json"]["messages"][0]["content"])
    assert "goal" not in user


def test_line_may_echo_the_shoppers_goal_words(monkeypatch, with_plan):
    use_settings(monkeypatch)
    # "Study" is not a catalog word: without the plan it would be an unknown proper noun.
    line = "Chips added, right on track for Study session fuel, and an Energy drink adds caffeine."
    fake_llm(monkeypatch, anthropic_reply(line))
    c = make_cart({"bar": 1})
    got, source, _ = agent.generate(c, MEMBER)
    assert (got, source) == (line, "llm")
    assert agent.validate(line, agent.policy(c, MEMBER), c, MEMBER) is None


def test_plan_lookup_never_breaks_the_line(monkeypatch):
    from backend import intent

    def boom(member_id):
        raise RuntimeError("intent is unavailable")

    monkeypatch.setattr(intent, "current_plan", boom)
    use_settings(monkeypatch)
    fake_llm(monkeypatch, anthropic_reply(GOOD))
    line, source, _ = agent.generate(make_cart({"bar": 1}), MEMBER)
    assert (line, source) == (GOOD, "llm")


def test_active_plan_without_a_member_id_is_none():
    assert agent.active_plan(None) is None
    assert agent.active_plan({}) is None
