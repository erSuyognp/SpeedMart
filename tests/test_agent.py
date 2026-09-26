"""S4.3 tests: AI store agent. Policy branches, templates, LLM validation and fallback, debounce. No network:
httpx.post is replaced in every test and fails the test if anything reaches it unexpectedly."""

from __future__ import annotations

import dataclasses
import json
import time

import httpx
import pytest

from backend import agent, cart, db, eventlog, shelf_state, store, ws
from backend.settings import settings

BUDGET = 20
FULL = {0: [0, 1], 1: [2, 3], 2: [4, 5]}
BASELINE = {"elx": 2, "rec": 2, "bar": 2}
MEMBER = {"id": "mem_test", "name": "Maya Lin", "budget_usd": BUDGET, "dietary": None}


def make_cart(picked: dict[str, int], budget: float = BUDGET, misplaced: list | None = None,
              session_id: str = "ses_test") -> dict:
    session = {"id": session_id, "state": "IN_STORE", "baseline": BASELINE}
    shelf = {sku: n - picked.get(sku, 0) for sku, n in BASELINE.items()}
    return cart.compute_cart(session, shelf, {}, {**MEMBER, "budget_usd": budget}, misplaced=misplaced)


def misplaced_bar() -> list[dict]:
    return [{"tag_id": 4, "sku": "bar", "name": "Protein bar", "bay": 0}]


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
    assert d == {"kind": "misplaced", "sku": "bar", "bay": 0, "name": "Protein bar"}


def test_policy_misplaced_beats_over_budget():
    c = make_cart({"elx": 2, "bar": 1}, misplaced=misplaced_bar())
    assert c["over_budget"]
    assert agent.policy(c, MEMBER)["kind"] == "misplaced"


def test_policy_over_budget_puts_back_most_expensive():
    c = make_cart({"elx": 2, "bar": 1})  # 19.50 + 1.56 tax = 21.06
    d = agent.policy(c, MEMBER)
    assert d == {"kind": "over_budget", "put_back": "elx", "put_back_name": "Electrolyte tabs", "over_by": 1.06}


def test_policy_suggests_complement_within_budget():
    d = agent.policy(make_cart({"elx": 1}), MEMBER)  # 8.64 now, 12.96 with the drink
    assert d == {"kind": "suggest", "sku": "rec", "sku_name": "Recovery drink", "cart_item": "Electrolyte tabs",
                 "price": 4.0, "remaining_after": 7.04}


def test_policy_no_suggestion_when_complement_breaks_budget():
    d = agent.policy(make_cart({"elx": 2}), MEMBER)  # 17.28 now, 21.60 with the drink (tax included)
    assert d == {"kind": "ok", "remaining": 2.72}


def test_policy_no_suggestion_when_complement_already_in_cart():
    assert agent.policy(make_cart({"elx": 1, "rec": 1}), MEMBER) == {"kind": "ok", "remaining": 7.04}


def test_policy_ok_without_complements():
    assert agent.policy(make_cart({"bar": 1}), MEMBER) == {"kind": "ok", "remaining": 16.22}


def test_policy_uses_member_budget():
    assert agent.policy(make_cart({"elx": 1}, budget=10), MEMBER)["kind"] == "ok"  # 12.96 > 10
    assert agent.policy(make_cart({"elx": 2}, budget=10), MEMBER)["kind"] == "over_budget"


# --- templates ---

@pytest.mark.parametrize("picked, misplaced, expected", [
    ({}, None, "Cart's empty. Grab anything from a lit bay."),
    ({"elx": 1}, misplaced_bar(), "Protein bar is in the wrong bay. Please return it to its lit slot."),
    ({"elx": 2, "bar": 1}, None, "You're $1.06 over budget. Putting back the Electrolyte tabs fixes it."),
    ({"elx": 1}, None, "Electrolyte tabs added. Recovery drink pairs well at $4 and keeps you under budget."),
    ({"bar": 1}, None, "Looking good. $16.22 left in your budget."),
])
def test_templates(picked, misplaced, expected):
    assert agent.template(agent.policy(make_cart(picked, misplaced=misplaced), MEMBER)) == expected


# --- LLM validation ---

GOOD = "Maya, Recovery drink pairs nicely with your Electrolyte tabs for $4 and keeps you under budget."


@pytest.mark.parametrize("raw", [
    "Line one.\nLine two.",
    " ".join(["word"] * 26),
    "Great pick \U0001F389 grab a Recovery drink for $4.",
    "Grab a Recovery drink for $4 ☕",
    "Try a Gatorade with your Electrolyte tabs, it keeps you under budget.",
    "A Recovery drink is only $3 and keeps you under budget.",
    "Add a #recovery drink for $4.",
    "",
    None,
    42,
])
def test_validate_rejects_bad_llm_output(raw):
    c = make_cart({"elx": 1})
    assert agent.validate(raw, agent.policy(c, MEMBER), c, MEMBER) is None


def test_validate_accepts_good_line():
    c = make_cart({"elx": 1})
    assert agent.validate(f'  "{GOOD}"  ', agent.policy(c, MEMBER), c, MEMBER) == GOOD


def test_validate_allows_exactly_25_words():
    c = make_cart({"bar": 1})
    line = " ".join(["good"] * 25)
    assert agent.validate(line, agent.policy(c, MEMBER), c, MEMBER) == line


# --- generate(): LLM phrasing with template fallback ---

def test_llm_line_used_when_valid(monkeypatch):
    use_settings(monkeypatch)
    calls = fake_llm(monkeypatch, anthropic_reply(GOOD))
    line, source, decision = agent.generate(make_cart({"elx": 1}), MEMBER)
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
    line, source, _ = agent.generate(make_cart({"elx": 1}), MEMBER)
    assert source == "template"
    assert line == "Electrolyte tabs added. Recovery drink pairs well at $4 and keeps you under budget."


def test_timeout_falls_back_to_template(monkeypatch):
    use_settings(monkeypatch)
    calls = fake_llm(monkeypatch, httpx.ReadTimeout("timed out"))
    line, source, _ = agent.generate(make_cart({"bar": 1}), MEMBER)
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
    line, source, _ = agent.generate(make_cart({"bar": 1}), MEMBER)
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
    line, source, _ = agent.generate(make_cart({"elx": 1}), MEMBER)  # no_network fails the test on any call
    assert source == "template" and line.startswith("Electrolyte tabs added.")


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
    a.on_cart(make_cart({"elx": 2, "bar": 1}), MEMBER)
    a.on_cart(make_cart({"bar": 1}), MEMBER)
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
    first = a.on_cart(make_cart({"elx": 1}), MEMBER)
    assert first["agent_line"] == ""  # nothing generated yet; on_cart never blocks
    assert wait_for(lambda: published)
    again = a.on_cart(make_cart({"elx": 1}), MEMBER)
    assert again["agent_line"] == "Electrolyte tabs added. Recovery drink pairs well at $4 and keeps you under budget."
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
            a.on_cart(make_cart({"bar": 1}), MEMBER)  # the shopper changes the cart mid-call
        return anthropic_reply("Looking good, $16.22 left.")

    monkeypatch.setattr(agent.httpx, "post", slow_post)
    a.on_cart(make_cart({"elx": 1}), MEMBER)
    assert wait_for(lambda: published)
    time.sleep(0.1)
    assert [m["data"]["line"] for m, _ in published] == ["Looking good, $16.22 left."]
    assert len(started) == 2


def test_on_cart_never_calls_llm_on_caller_thread(monkeypatch, published):
    use_settings(monkeypatch)
    fake_llm(monkeypatch, httpx.ReadTimeout("slow"))
    a = agent.Agent(debounce_s=0.01)
    t0 = time.monotonic()
    a.on_cart(make_cart({"elx": 1}), MEMBER)
    assert time.monotonic() - t0 < 0.05
    assert wait_for(lambda: published)
    assert published[0][0]["data"]["line"].startswith("Electrolyte tabs added.")


# --- wired into the store: pick electrolytes, the snapshot carries the suggestion ---

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
    shelf_state.apply_snapshot(snap({0: [1], 1: [2, 3], 2: [4, 5]}, 2))  # electrolytes picked
    store.on_shelf_change()
    expected = "Electrolyte tabs added. Recovery drink pairs well at $4 and keeps you under budget."
    assert wait_for(lambda: any(m["type"] == "agent" and m["data"]["line"] == expected for m, _ in published))
    assert store.current_cart()["agent_line"] == expected
    lines = [json.loads(x) for x in eventlog.EVENTS_PATH.read_text(encoding="utf-8").splitlines()]
    assert any(e["type"] == "agent_line" and e["decision"]["kind"] == "suggest" for e in lines)


# --- F18: the active intent plan reaches the agent line -------------------------------------------

PLAN = {
    "plan_id": "pln_test",
    "goal_summary": "post run recovery",
    "items": [{"sku": "elx", "name": "Electrolyte tabs", "qty": 1},
              {"sku": "rec", "name": "Recovery drink", "qty": 1}],
    "est_total_usd": 12.96,
    "budget_usd": 15.0,
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
    agent.generate(make_cart({"elx": 1}), MEMBER)
    user = json.loads(calls[0]["json"]["messages"][0]["content"])
    assert user["goal"] == {"summary": "post run recovery",
                            "items": ["Electrolyte tabs", "Recovery drink"]}
    # The plan's own money never goes in the prompt: validate() would reject any amount it did not supply.
    assert "12.96" not in calls[0]["json"]["messages"][0]["content"]


def test_no_goal_key_without_a_plan(monkeypatch):
    use_settings(monkeypatch)
    calls = fake_llm(monkeypatch, anthropic_reply(GOOD))
    agent.generate(make_cart({"elx": 1}), MEMBER)
    user = json.loads(calls[0]["json"]["messages"][0]["content"])
    assert "goal" not in user


def test_line_may_echo_the_shoppers_goal_words(monkeypatch, with_plan):
    use_settings(monkeypatch)
    # "Recovery" is a catalog word, but a goal word like this would otherwise be an unknown proper noun.
    line = "Electrolyte tabs added, right on track for Post run recovery under budget."
    fake_llm(monkeypatch, anthropic_reply(line))
    got, source, _ = agent.generate(make_cart({"elx": 1}), MEMBER)
    assert (got, source) == (line, "llm")


def test_plan_lookup_never_breaks_the_line(monkeypatch):
    from backend import intent

    def boom(member_id):
        raise RuntimeError("intent is unavailable")

    monkeypatch.setattr(intent, "current_plan", boom)
    use_settings(monkeypatch)
    fake_llm(monkeypatch, anthropic_reply(GOOD))
    line, source, _ = agent.generate(make_cart({"elx": 1}), MEMBER)
    assert (line, source) == (GOOD, "llm")


def test_active_plan_without_a_member_id_is_none():
    assert agent.active_plan(None) is None
    assert agent.active_plan({}) is None
