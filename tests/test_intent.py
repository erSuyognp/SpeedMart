"""F18 tests: intent planner, validation, rules fallback, highlights, cart vs plan. No network, no board."""

from __future__ import annotations

import dataclasses
import json

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from backend import db, eventlog, intent, serial_bridge
from backend.settings import settings

ROOT = Path(__file__).resolve().parent.parent
CATALOG = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))


def skus_tagged(tag: str) -> list[str]:
    """Every SKU carrying a catalog tag, in catalog order: what the rules planner matches on."""
    return [s["sku"] for s in CATALOG["skus"] if tag in s["tags"]]


# --- fixtures ---


@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "speedmart.db")
    monkeypatch.setattr(eventlog, "DATA_DIR", tmp_path)
    monkeypatch.setattr(eventlog, "EVENTS_PATH", tmp_path / "events.log.jsonl")
    db.init_db()
    intent._plans.clear()
    yield tmp_path
    intent._plans.clear()


def set_features(monkeypatch, **flags):
    """Settings are frozen: swap the copy the intent module reads (same pattern as test_serial.py)."""
    cur = intent.settings
    monkeypatch.setattr(intent, "settings", dataclasses.replace(cur, features=dataclasses.replace(cur.features, **flags)))


def set_env(monkeypatch, **values):
    cur = intent.settings
    monkeypatch.setattr(intent, "settings", dataclasses.replace(cur, env=dataclasses.replace(cur.env, **values)))


@pytest.fixture
def llm_openai(monkeypatch):
    set_features(monkeypatch, llm=True)
    set_env(monkeypatch, llm_provider="openai", openai_api_key="test-key", openai_model="test-model",
            openai_base_url="https://llm.example/v1")


@pytest.fixture
def llm_off(monkeypatch):
    set_features(monkeypatch, llm=False)


def add_member(budget=20.0, dietary=None) -> str:
    member_id = db.new_id("mem")
    conn = db.connect()
    try:
        conn.execute("INSERT INTO members (id, name, budget_usd, dietary, created_at) VALUES (?, ?, ?, ?, ?)",
                     (member_id, "Maya Test", budget, dietary, db.now_iso()))
        conn.commit()
    finally:
        conn.close()
    return member_id


def make_client() -> TestClient:
    """Own test app: only the intent router plus a helper route that logs a member in."""
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test")
    app.include_router(intent.router)

    @app.post("/test/login/{member_id}")
    def login(member_id: str, request: Request):
        request.session["member_id"] = member_id
        return {"ok": True}

    return TestClient(app)


def logged_in_client(member_id: str) -> TestClient:
    c = make_client()
    c.post(f"/test/login/{member_id}")
    return c


def openai_reply(content: str):
    def fake_post(url, **kwargs):
        fake_post.calls.append((url, kwargs))
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]},
                              request=httpx.Request("POST", url))
    fake_post.calls = []
    return fake_post


def events(event_type: str) -> list[dict]:
    if not eventlog.EVENTS_PATH.exists():
        return []
    rows = [json.loads(line) for line in eventlog.EVENTS_PATH.read_text(encoding="utf-8").splitlines()]
    return [r for r in rows if r["type"] == event_type]


GOOD = {"goal_summary": "run recovery",
        "items": [{"sku": "elx", "qty": 1, "reason": "Replaces salts lost on your run"},
                  {"sku": "rec", "qty": 1, "reason": "Pairs with electrolytes for recovery"}],
        "budget_override_usd": 15}

# --- LLM path ---


def test_llm_success_path(tmp_data, llm_openai, monkeypatch):
    fake = openai_reply("```json\n" + json.dumps(GOOD) + "\n```")  # fences must be stripped
    monkeypatch.setattr(intent.httpx, "post", fake)
    c = logged_in_client(add_member())
    r = c.post("/api/intent", json={"text": "recovering from a run, under $15"})
    assert r.status_code == 200
    plan = r.json()
    assert plan["source"] == "llm"
    assert plan["goal_summary"] == "run recovery"
    assert [(i["sku"], i["qty"]) for i in plan["items"]] == [("elx", 1), ("rec", 1)]
    assert plan["items"][0]["name"] == "Electrolyte tabs" and plan["items"][0]["unit_price_usd"] == 8.0
    assert plan["est_total_usd"] == 12.96
    assert plan["budget_usd"] == 15.0 and plan["fits_budget"] is True
    assert plan["bays"] == [0, 1]
    assert plan["plan_id"].startswith("pln_") and plan["created_at"]
    # request shape: OpenAI compatible, 3 s timeout, catalog + budget + text sent
    url, kwargs = fake.calls[0]
    assert url == "https://llm.example/v1/chat/completions"
    assert kwargs["timeout"] == 3.0
    sent = json.loads(kwargs["json"]["messages"][1]["content"])
    assert {s["sku"] for s in sent["catalog"]} == {s["sku"] for s in CATALOG["skus"]}
    assert sent["budget_usd"] == 20.0 and "run" in sent["text"]
    # stored and logged
    assert c.get("/api/intent/current").json()["plan_id"] == plan["plan_id"]
    logged = events("intent_plan")
    assert logged and logged[-1]["source"] == "llm" and logged[-1]["plan_id"] == plan["plan_id"]


def test_anthropic_provider(tmp_data, monkeypatch):
    set_features(monkeypatch, llm=True)
    set_env(monkeypatch, llm_provider="anthropic  # anthropic | openai", anthropic_api_key="k",
            anthropic_model="claude-haiku-4-5")

    def fake_post(url, **kwargs):
        assert url == "https://api.anthropic.com/v1/messages"
        assert kwargs["headers"]["x-api-key"] == "k" and kwargs["timeout"] == 3.0
        return httpx.Response(200, json={"content": [{"type": "text", "text": json.dumps(GOOD)}]},
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(intent.httpx, "post", fake_post)
    plan = logged_in_client(add_member()).post("/api/intent", json={"text": "run recovery"}).json()
    assert plan["source"] == "llm"


def test_invalid_llm_json_falls_back_to_rules(tmp_data, llm_openai, monkeypatch):
    monkeypatch.setattr(intent.httpx, "post", openai_reply("Sure! Here is your plan: electrolytes."))
    plan = logged_in_client(add_member()).post("/api/intent", json={"text": "Post run recovery under $15"}).json()
    assert plan["source"] == "rules"
    assert [i["sku"] for i in plan["items"]] == ["elx", "rec"]
    assert plan["est_total_usd"] <= plan["budget_usd"] == 15.0
    assert events("intent_plan")[-1]["fallback_reason"].startswith("invalid:not_json")


def test_llm_timeout_falls_back(tmp_data, llm_openai, monkeypatch):
    def slow(url, **kwargs):
        raise httpx.ReadTimeout("timed out")
    monkeypatch.setattr(intent.httpx, "post", slow)
    plan = logged_in_client(add_member()).post("/api/intent", json={"text": "Quick protein snack"}).json()
    # the rules planner picks every snack on the shelf; all of them fit the $20 budget
    assert plan["source"] == "rules" and [i["sku"] for i in plan["items"]] == skus_tagged("snack")
    assert events("intent_plan")[-1]["fallback_reason"] == "llm_error:ReadTimeout"


def test_llm_off_uses_rules_without_calling(tmp_data, llm_off, monkeypatch):
    def boom(url, **kwargs):
        raise AssertionError("LLM must not be called when the llm flag is off")
    monkeypatch.setattr(intent.httpx, "post", boom)
    plan = logged_in_client(add_member()).post("/api/intent", json={"text": "Something to drink"}).json()
    assert plan["source"] == "rules"
    assert set(skus_tagged("drink")) <= {i["sku"] for i in plan["items"]}


# --- strict validation ---

MEMBER = {"budget_usd": 20.0, "dietary": None}


def reply(**overrides):
    return {**GOOD, "budget_override_usd": None, **overrides}


def test_unknown_sku_rejected():
    data = reply(items=[{"sku": "caviar", "qty": 1, "reason": "Fancy"}])
    with pytest.raises(intent.PlanInvalid, match="unknown_sku"):
        intent.validate_llm_plan(data, "something fancy", MEMBER)


def test_unknown_sku_via_route_falls_back(tmp_data, llm_openai, monkeypatch):
    monkeypatch.setattr(intent.httpx, "post", openai_reply(json.dumps(reply(
        items=[{"sku": "chips", "qty": 1, "reason": "Crunchy"}]))))
    plan = logged_in_client(add_member()).post("/api/intent", json={"text": "a snack"}).json()
    assert plan["source"] == "rules"
    assert all(i["sku"] in settings.skus for i in plan["items"])


def test_over_budget_rejected():
    data = reply(items=[{"sku": "elx", "qty": 2, "reason": "Two"}, {"sku": "rec", "qty": 1, "reason": "One"}])
    # 2 x 8.00 + 4.00 = 20.00, with 8% tax 21.60 > 20
    with pytest.raises(intent.PlanInvalid, match="over_budget"):
        intent.validate_llm_plan(data, "recovery", MEMBER)


def test_qty_above_stock_rejected():
    with pytest.raises(intent.PlanInvalid, match="bad_qty"):
        intent.validate_llm_plan(reply(items=[{"sku": "bar", "qty": 3, "reason": "Lots"}]), "bars", MEMBER)
    with pytest.raises(intent.PlanInvalid, match="bad_qty"):
        intent.validate_llm_plan(reply(items=[{"sku": "bar", "qty": 0, "reason": "None"}]), "bars", MEMBER)
    with pytest.raises(intent.PlanInvalid, match="bad_qty"):
        intent.validate_llm_plan(reply(items=[{"sku": "bar", "qty": "1", "reason": "Str"}]), "bars", MEMBER)


def test_long_reason_rejected():
    long = "this reason is far too long because it keeps going on and on forever"
    with pytest.raises(intent.PlanInvalid, match="reason_too_long"):
        intent.validate_llm_plan(reply(items=[{"sku": "bar", "qty": 1, "reason": long}]), "bar", MEMBER)


def test_duplicate_sku_and_empty_items_rejected():
    dup = [{"sku": "bar", "qty": 1, "reason": "A"}, {"sku": "bar", "qty": 1, "reason": "B"}]
    with pytest.raises(intent.PlanInvalid, match="duplicate_sku"):
        intent.validate_llm_plan(reply(items=dup), "bars", MEMBER)
    with pytest.raises(intent.PlanInvalid, match="items_missing"):
        intent.validate_llm_plan(reply(items=[]), "bars", MEMBER)


# --- budget override ---


def test_budget_from_text():
    assert intent.budget_from_text("recovering from a run, under $15") == 1500
    assert intent.budget_from_text("snacks for two, 10 bucks") == 1000
    assert intent.budget_from_text("budget of 7.50") == 750
    assert intent.budget_from_text("under 12 dollars or $9") == 900
    assert intent.budget_from_text("vegan snacks for two") is None


def test_budget_override_only_lowers():
    assert intent.effective_budget_cents(20, "run stuff", 15) == 1500  # LLM read a lower budget
    assert intent.effective_budget_cents(20, "run stuff", 50) == 2000  # never raised above the member budget
    assert intent.effective_budget_cents(20, "under $12", None) == 1200  # text wins even if LLM missed it
    assert intent.effective_budget_cents(20, "under $12", 14) == 1200
    assert intent.effective_budget_cents(20, "run", True) == 2000  # bool is not a number


def test_budget_override_applied_to_validation():
    # elx + rec = 12.96 fits $20 but not "under $10"
    with pytest.raises(intent.PlanInvalid, match="over_budget"):
        intent.validate_llm_plan(reply(budget_override_usd=10), "recovery, ten max", MEMBER)
    with pytest.raises(intent.PlanInvalid, match="over_budget"):
        intent.validate_llm_plan(reply(), "recovery under $10", MEMBER)
    summary, lines, reasons, budget = intent.validate_llm_plan(reply(budget_override_usd=15), "recovery", MEMBER)
    assert budget == 1500 and lines == {"elx": 1, "rec": 1}
    with pytest.raises(intent.PlanInvalid, match="budget_override_not_a_number"):
        intent.validate_llm_plan(reply(budget_override_usd="15"), "recovery", MEMBER)


def test_rules_respect_budget():
    _, lines, _, budget = intent.rules_plan("recovering from a run, under $10", MEMBER)
    assert budget == 1000 and lines == {"elx": 1}
    _, lines, _, _ = intent.rules_plan("vegan snacks for two", MEMBER)
    assert lines == {sku: 2 for sku in skus_tagged("snack")}  # two of each snack still fits $20
    _, lines, _, _ = intent.rules_plan("protein snack under $3", MEMBER)
    assert lines == {}  # nothing fits: an empty plan, never an over-budget one
    _, lines, _, _ = intent.rules_plan("caviar", MEMBER)
    assert lines == {}


def test_member_budget_from_db(tmp_data, llm_off):
    plan = logged_in_client(add_member(budget=5.0)).post("/api/intent", json={"text": "recovery drink"}).json()
    assert plan["budget_usd"] == 5.0
    assert plan["est_total_usd"] <= 5.0
    assert [i["sku"] for i in plan["items"]] == ["rec"]  # elx ($8.64) cannot fit


# --- routes ---


def test_routes_require_login(tmp_data):
    c = make_client()
    assert c.post("/api/intent", json={"text": "snack"}).status_code == 401
    assert c.get("/api/intent/current").json()["error"] == "not_logged_in"
    assert c.delete("/api/intent").status_code == 401


def test_empty_text_rejected(tmp_data):
    r = logged_in_client(add_member()).post("/api/intent", json={"text": "   "})
    assert r.status_code == 400 and r.json()["error"] == "empty_text"


def test_current_and_delete(tmp_data, llm_off):
    c = logged_in_client(add_member())
    assert c.get("/api/intent/current").json() is None
    plan = c.post("/api/intent", json={"text": "snack"}).json()
    assert c.get("/api/intent/current").json() == plan
    assert c.delete("/api/intent").json() == {"ok": True}
    assert c.get("/api/intent/current").json() is None
    assert events("intent_cleared")


def test_plans_are_per_member(tmp_data, llm_off):
    a, b = logged_in_client(add_member()), logged_in_client(add_member())
    a.post("/api/intent", json={"text": "snack"})
    assert b.get("/api/intent/current").json() is None


# --- highlights ---


def test_highlight_helpers_missing_does_not_crash(tmp_data, llm_off, monkeypatch):
    set_features(monkeypatch, hardware_leds=True)
    monkeypatch.delattr(serial_bridge, "highlight", raising=False)
    monkeypatch.delattr(serial_bridge, "clear_highlights", raising=False)
    c = logged_in_client(add_member())
    assert c.post("/api/intent", json={"text": "snack"}).status_code == 200
    assert c.delete("/api/intent").status_code == 200
    eventlog.log("session_state", session_id="ses_x", member_id="m", **{"from": "IN_STORE"}, to="CANCELLED")


def fake_helpers(monkeypatch):
    calls = []
    monkeypatch.setattr(serial_bridge, "highlight", lambda bay, on: calls.append(("highlight", bay, on)),
                        raising=False)
    monkeypatch.setattr(serial_bridge, "clear_highlights", lambda: calls.append(("clear",)), raising=False)
    return calls


def test_highlights_called_when_present(tmp_data, llm_off, monkeypatch):
    set_features(monkeypatch, hardware_leds=True)
    calls = fake_helpers(monkeypatch)
    c = logged_in_client(add_member())
    c.post("/api/intent", json={"text": "Post run recovery under $15"})
    assert calls == [("clear",), ("highlight", 0, True), ("highlight", 1, True)]
    calls.clear()
    c.delete("/api/intent")
    assert calls == [("clear",)]


def test_highlights_cleared_on_session_end(tmp_data, monkeypatch):
    set_features(monkeypatch, hardware_leds=True)
    calls = fake_helpers(monkeypatch)
    eventlog.log("session_state", session_id="ses_x", member_id="m", **{"from": "CHECKOUT_PENDING"}, to="PAID")
    assert calls == [("clear",)]
    calls.clear()
    eventlog.log("session_state", session_id="ses_x", member_id="m", **{"from": "IN_STORE"}, to="CHECKOUT_PENDING")
    assert calls == []


def test_new_entry_forgets_old_plan(tmp_data, llm_off):
    member_id = add_member()
    c = logged_in_client(member_id)
    c.post("/api/intent", json={"text": "snack"})
    eventlog.log("session_state", session_id="ses_y", member_id=member_id, **{"from": None}, to="IN_STORE")
    assert c.get("/api/intent/current").json() is None


def test_highlight_helper_error_is_swallowed(tmp_data, llm_off, monkeypatch):
    set_features(monkeypatch, hardware_leds=True)

    def broken(*args):
        raise RuntimeError("port gone")
    monkeypatch.setattr(serial_bridge, "highlight", broken, raising=False)
    monkeypatch.setattr(serial_bridge, "clear_highlights", broken, raising=False)
    assert logged_in_client(add_member()).post("/api/intent", json={"text": "snack"}).status_code == 200
    assert events("intent_highlight_error")


def test_leds_off_never_calls_helpers(tmp_data, llm_off, monkeypatch):
    set_features(monkeypatch, hardware_leds=False)
    calls = fake_helpers(monkeypatch)
    c = logged_in_client(add_member())
    c.post("/api/intent", json={"text": "snack"})
    c.delete("/api/intent")
    assert calls == []


# --- compare_cart_to_plan ---

PLAN = {"goal_summary": "run recovery", "budget_usd": 15.0,
        "items": [{"sku": "elx", "name": "Electrolyte tabs", "qty": 1},
                  {"sku": "rec", "name": "Recovery drink", "qty": 1}]}


def cart(*lines, total):
    names = {s.sku: s.name for s in settings.skus.values()}
    return {"items": [{"sku": sku, "name": names[sku], "qty": q} for sku, q in lines], "total_usd": total}


def test_compare_exact_match():
    r = intent.compare_cart_to_plan(cart(("elx", 1), ("rec", 1), total=12.96), PLAN)
    assert r == {"matches": True, "missing": [], "extra": [],
                 "summary": "You asked for run recovery under $15: you have both items, $12.96."}


def test_compare_missing():
    r = intent.compare_cart_to_plan(cart(("elx", 1), total=8.64), PLAN)
    assert r["matches"] is False
    assert r["missing"] == [{"sku": "rec", "name": "Recovery drink", "qty": 1}] and r["extra"] == []
    assert r["summary"] == "You asked for run recovery under $15: you still need Recovery drink, $8.64."


def test_compare_extra_and_over_budget():
    r = intent.compare_cart_to_plan(cart(("elx", 1), ("rec", 1), ("bar", 1), total=16.74), PLAN)
    assert r["matches"] is False and r["missing"] == []
    assert r["extra"] == [{"sku": "bar", "name": "Protein bar", "qty": 1}]
    assert r["summary"] == ("You asked for run recovery under $15: you have everything, plus Protein bar, "
                            "$16.74, over your $15 budget.")


def test_compare_missing_and_extra_and_qty_diff():
    r = intent.compare_cart_to_plan(cart(("elx", 2), ("bar", 1), total=21.06), PLAN)
    assert r["missing"] == [{"sku": "rec", "name": "Recovery drink", "qty": 1}]
    assert r["extra"] == [{"sku": "elx", "name": "Electrolyte tabs", "qty": 1},
                          {"sku": "bar", "name": "Protein bar", "qty": 1}]
    assert "you're missing Recovery drink and also picked Electrolyte tabs and Protein bar" in r["summary"]


def test_compare_empty_cart_and_no_plan():
    r = intent.compare_cart_to_plan(cart(total=0), PLAN)
    assert r["matches"] is False and len(r["missing"]) == 2
    assert r["summary"] == "You asked for run recovery under $15: you haven't picked anything up yet."
    assert intent.compare_cart_to_plan(cart(total=0), None)["matches"] is False
    assert intent.compare_cart_to_plan(None, None)["summary"] == "No shopping plan for this visit."


def test_compare_single_item_and_cents_budget():
    plan = {"goal_summary": "a quick snack", "budget_usd": 7.5,
            "items": [{"sku": "bar", "name": "Protein bar", "qty": 1}]}
    r = intent.compare_cart_to_plan(cart(("bar", 1), total=3.78), plan)
    assert r["summary"] == "You asked for a quick snack under $7.50: you have it, $3.78."


def test_compare_is_pure():
    snap = cart(("elx", 1), total=8.64)
    before = json.dumps([snap, PLAN], sort_keys=True)
    intent.compare_cart_to_plan(snap, PLAN)
    assert json.dumps([snap, PLAN], sort_keys=True) == before
