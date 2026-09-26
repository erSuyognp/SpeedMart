"""AI assisted dispute review with a human in the loop (8.14): JSON validation, the unclear band, banned words,
the auto approve policy boundaries, "the AI never denies", the admin decision flow, the timeout fallback and the
startup probe. The model is mocked everywhere; no test reaches the network."""

from __future__ import annotations

import dataclasses
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import admin, db, disputes, review, store
from backend.main import app
from identity_helpers import login_as, set_env, set_features
from test_cart import FULL, events, member_id, qty, tmp_data, with_bays  # noqa: F401
from test_disputes import crop, dispute, enter
from test_returns import EXIT, buy, face_id, points_of, refund_rows, shelf

pytestmark = pytest.mark.usefixtures("tmp_data")

BAR_TAKEN = with_bays(b2=[5])  # one bag of chips: $2.50 + $0.20 tax = $2.70, under the $5 auto approve line
GOOD = json.dumps({"supports_customer_pct": 92, "verdict": "supports_customer",
                   "summary": "Tag 0 is visible in every keyframe; the unit never left bay 1.",
                   "evidence": [{"frame": "baseline", "observation": "Both units present."},
                                {"frame": "latest", "observation": "Both units still present."}]})
CHARGE = json.dumps({"supports_customer_pct": 4, "verdict": "supports_charge",
                     "summary": "Tag 0 leaves bay 1 during the motion period and does not return.",
                     "evidence": [{"frame": "latest", "observation": "Only tag 1 remains."}]})
# Supports the customer, but under the 80 % auto approve line: a person decides whatever the amount. Every single
# unit in the catalog is now under $5, so this is how the admin decision tests reach a person.
LEANS = json.dumps({"supports_customer_pct": 72, "verdict": "supports_customer",
                    "summary": "Tag 0 is probably still in bay 1; one keyframe is blurred.",
                    "evidence": [{"frame": "latest", "observation": "Tag 0 likely present."}]})


real_probe = review.probe


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    set_features(monkeypatch, gates=True, passkeys=True, stripe=False, loyalty=True, disputes=True, llm=True)
    monkeypatch.setattr(admin, "force_decline", False)
    monkeypatch.setattr(review, "vision_ok", None)
    monkeypatch.setattr(review, "schedule", review.run)  # no background thread in tests
    monkeypatch.setattr(review, "probe", lambda: None)  # the app's startup probe: only the probe tests run it


BAY2_TAGS = {"4": [20.0, 40.0, 60.0, 60.0], "5": [120.0, 40.0, 60.0, 60.0]}


def bay2_photos(client: TestClient, sid: str) -> None:
    """Baseline of bay 2 (chips) with both units, then tag 5 gone. Only while the visit is shopping."""
    assert crop(client, sid, bay=2, kind="baseline", units=(4, 5), tags=BAY2_TAGS, ts=1_800_000_000_000)[0].status_code == 200
    assert crop(client, sid, bay=2, kind="change", units=(4,), tags={"4": BAY2_TAGS["4"]},
                ts=1_800_000_005_000)[0].status_code == 200


def buy_bar_with_photos(client: TestClient, mid: str) -> str:
    """Enter, take one bag of chips (photos of its bay saved), pay. Returns the paid session id."""
    sid = enter(client, mid, taken=BAR_TAKEN)
    bay2_photos(client, sid)
    assert client.post("/api/gate/exit/quote", json=EXIT).status_code == 200
    face_id(client, mid)
    r = client.post("/api/gate/exit/approve")
    assert r.status_code == 200 and r.json()["payment"]["status"] == "AUTHORIZED", r.text
    return sid


def with_model(monkeypatch, replies):
    """A configured model (Anthropic key) whose call_model returns the given replies in turn, or raises them."""
    set_env(monkeypatch, review_provider="anthropic", review_model="", anthropic_api_key="test-key",
            anthropic_model="test-vision-model")
    calls = []
    queue = list(replies)

    def fake(system, text, images, timeout=review.REVIEW_TIMEOUT_S):
        calls.append({"system": system, "text": text, "images": list(images), "timeout": timeout})
        reply = queue.pop(0) if queue else replies[-1]
        if isinstance(reply, BaseException):
            raise reply
        return reply

    monkeypatch.setattr(review, "call_model", fake)
    return calls


# --- JSON validation ---

def test_validate_accepts_strict_json_and_code_fences():
    out = review.validate("```json\n" + GOOD + "\n```", {"baseline", "latest"})
    assert out == {"supports_customer_pct": 92, "verdict": "supports_customer",
                   "summary": "Tag 0 is visible in every keyframe; the unit never left bay 1.",
                   "evidence": [{"frame": "baseline", "observation": "Both units present."},
                                {"frame": "latest", "observation": "Both units still present."}]}
    # prose around the object is tolerated; nothing else is
    assert review.validate("Sure! " + GOOD + " Hope this helps.", {"baseline", "latest"})["supports_customer_pct"] == 92
    with pytest.raises(ValueError):
        review.validate("I cannot tell.", set())
    with pytest.raises(ValueError):
        review.validate(json.dumps({"verdict": "unclear", "summary": "no number"}), set())


def test_verdict_is_unclear_across_the_band_and_follows_the_percentage_outside_it():
    def v(pct, given="supports_customer"):
        return review.validate({"supports_customer_pct": pct, "verdict": given, "summary": "x"}, set())["verdict"]
    assert v(34, "unclear") == "supports_charge"
    assert v(35, "supports_customer") == "unclear"
    assert v(50, "supports_charge") == "unclear"
    assert v(65, "supports_customer") == "unclear"
    assert v(66, "unclear") == "supports_customer"
    assert v(100, "supports_charge") == "supports_customer"  # a verdict that contradicts its number never gets out
    assert v(0, "supports_customer") == "supports_charge"
    # the percentage is clamped and rounded
    assert review.validate({"supports_customer_pct": 140.4, "summary": "x"}, set())["supports_customer_pct"] == 100
    assert review.validate({"supports_customer_pct": -3, "summary": "x"}, set())["supports_customer_pct"] == 0
    assert review.validate({"supports_customer_pct": "79.6", "summary": "x"}, set())["supports_customer_pct"] == 80


def test_banned_words_are_replaced_everywhere_and_texts_are_capped():
    raw = {"supports_customer_pct": 10, "verdict": "supports_charge",
           "summary": "The shopper is LYING and cheating; this looks like fraud, they stole it. " + "word " * 60,
           "evidence": [{"frame": "latest", "observation": "A thief steals the bar (theft)."},
                        {"frame": "nope", "observation": "unknown frame: dropped"},
                        {"frame": "baseline", "observation": ""},
                        "not a dict"]}
    out = review.validate(raw, {"baseline", "latest"})
    for word in ("lying", "cheat", "fraud", "stole", "thief", "steal", "theft"):
        assert word not in out["summary"].lower() and word not in json.dumps(out["evidence"]).lower()
    assert out["summary"].startswith("The shopper is discrepancy and discrepancy; this looks like discrepancy, they discrepancy it.")
    assert len(out["summary"].split()) == 40
    assert out["evidence"] == [{"frame": "latest", "observation": "A discrepancy discrepancy the bar (discrepancy)."}]
    assert review.sanitize("  Nothing   odd here  ") == "Nothing odd here"


# --- the policy (code, never the model) ---

def test_auto_approve_policy_boundaries():
    def p(pct, cents, verdict="supports_customer"):
        return review.policy({"verdict": verdict, "supports_customer_pct": pct}, cents)
    assert p(80, 499) == "auto_approve"
    assert p(80, 500) == "human"  # $5.00 is not under $5.00
    assert p(79, 499) == "human"
    assert p(100, 499) == "auto_approve"
    assert p(100, 499, "unclear") == "human"
    assert p(100, 1, "supports_charge") == "human"  # never a denial, whatever the number
    assert p(80, 0) == "auto_approve"


def test_unavailable_review_shape():
    u = review.unavailable("timeout")
    assert (u["verdict"], u["supports_customer_pct"], u["summary"], u["evidence"]) == ("unclear", 50, "AI review unavailable", [])
    assert review.policy(u, 1) == "human"


# --- reviews on real disputes ---

def review_of(dispute_id: str) -> dict:
    return json.loads(disputes._row(dispute_id)["review_json"])


def test_small_clear_case_is_refunded_at_once_by_the_policy(member_id, monkeypatch):
    calls = with_model(monkeypatch, [GOOD])
    with TestClient(app) as client:
        sid = buy_bar_with_photos(client, member_id)
        points = points_of(member_id)
        r = dispute(client, "bar", sid)
        assert r.status_code == 200, r.text
        body = r.json()
        d = body["dispute"]
        assert d["outcome"] == "refunded" and d["status"] == "resolved" and d["amount_usd"] == 2.70
        assert d["message"] == "Refunded $2.70 after a review of the shelf photos."
        assert d["customer_status"] == {"label": "Refunded", "note": None}
        assert body["refund"]["reason"] == "ai_auto_small" and body["refund"]["status"] == "SUCCEEDED"
        assert refund_rows()[-1]["reason"] == "ai_auto_small" and refund_rows()[-1]["dispute_id"] == d["dispute_id"]
        assert points_of(member_id) == points - 2
        # the model got the photos and the timeline
        assert len(calls) == 1 and len(calls[0]["images"]) == 2 and calls[0]["timeout"] == 20
        sent = json.loads(calls[0]["text"])
        assert [f["id"] for f in sent["frames"]] == ["baseline", "latest"]
        assert sent["timeline"]["item"] == "Chips" and sent["timeline"]["missing_tag_id"] == 5
        assert sent["timeline"]["baseline_units"] == [4, 5] and sent["timeline"]["latest_units"] == [4]
        assert "cheat" not in calls[0]["system"].lower().replace("cheat, fraud", "")  # only in the "never use" rule
        stored = review_of(d["dispute_id"])
        assert stored["verdict"] == "supports_customer" and stored["source"] == "vision" and stored["attempts"] == 1
        decision = json.loads(disputes._row(d["dispute_id"])["decision_json"])
        assert decision["by"] == "ai" and decision["decision"] == "approve" and decision["reason"] == "ai_auto_small"
        assert events("dispute_policy")[-1]["decision"] == "auto_approve"
        assert events("dispute_decision")[-1]["by"] == "ai"
        # the receipt tells the shopper
        receipt = client.get(f"/api/receipt/{sid}").json()
        assert receipt["disputes"][0]["customer_status"]["label"] == "Refunded"
        assert receipt["refunds"][0]["reason"] == "ai_auto_small"
        # metrics
        login_as(client, member_id, admin=True)
        m = client.get("/admin/state").json()["metrics"]
        assert (m["disputes_today"], m["auto_approved_today"], m["ai_human_agreement_pct"]) == (1, 1, None)


def test_a_clear_case_over_five_dollars_waits_for_a_person(member_id, monkeypatch):
    with_model(monkeypatch, [GOOD])
    # Every unit in the catalog is under $5 and a dispute is always one unit, so price the hydration drink at
    # $5.00 for this visit: $5.00 + $0.40 tax = $5.40, over the line.
    monkeypatch.setitem(review.settings.skus, "elx", dataclasses.replace(review.settings.skus["elx"], price_usd=5.00))
    with TestClient(app) as client:
        sid = buy(client, member_id)  # one hydration drink: $5.40
        d = dispute(client, "elx", sid).json()["dispute"]
        assert d["outcome"] == "needs_review" and d["status"] == "open" and d["disputed_usd"] == 5.40
        assert d["customer_status"] == {"label": "Under review", "note": None}
        assert refund_rows() == []
        assert events("dispute_policy")[-1]["decision"] == "human"
        login_as(client, member_id, admin=True)
        listed = client.get("/admin/state").json()["disputes"][0]
        assert listed["review"]["supports_customer_pct"] == 92 and listed["review"]["verdict"] == "supports_customer"
        assert listed["review"]["evidence"] == [] and listed["decision"] is None  # no photos: frame ids unknown, dropped


def test_the_ai_can_never_deny_a_dispute(member_id, monkeypatch):
    with_model(monkeypatch, [CHARGE])
    with TestClient(app) as client:
        sid = buy(client, member_id, taken=BAR_TAKEN)
        d = dispute(client, "bar", sid).json()["dispute"]
        assert d["status"] == "open" and d["outcome"] == "needs_review"
        assert review_of(d["dispute_id"])["verdict"] == "supports_charge"
        assert disputes._row(d["dispute_id"])["decision_json"] is None  # nobody decided anything
        assert client.get(f"/api/receipt/{sid}").json()["disputes"][0]["customer_status"]["label"] == "Under review"
        # and the shopper can still remove it themselves, as before
        assert client.post(f"/api/disputes/{d['dispute_id']}/remove").json()["dispute"]["outcome"] == "refunded"


def test_no_model_configured_means_unavailable_at_once(member_id):
    with TestClient(app) as client:
        sid = buy(client, member_id, taken=BAR_TAKEN)
        d = dispute(client, "bar", sid).json()["dispute"]
        assert d["status"] == "open"
        stored = review_of(d["dispute_id"])
        assert stored["summary"] == "AI review unavailable" and stored["verdict"] == "unclear" and stored["source"] == "unavailable"


def test_timeout_then_retry_then_unavailable(member_id, monkeypatch):
    calls = with_model(monkeypatch, [httpx.ReadTimeout("slow"), httpx.ReadTimeout("slower")])
    with TestClient(app) as client:
        sid = buy(client, member_id, taken=BAR_TAKEN)
        d = dispute(client, "bar", sid).json()["dispute"]
        assert d["status"] == "open"
        stored = review_of(d["dispute_id"])
        assert stored["summary"] == "AI review unavailable" and stored["verdict"] == "unclear"
        assert stored["supports_customer_pct"] == 50 and "ReadTimeout" in stored["error"]
        assert len(calls) == 2  # one retry, then give up
        assert [e["attempt"] for e in events("review_attempt_failed")] == [1, 2]


def test_one_bad_reply_then_a_good_one(member_id, monkeypatch):
    calls = with_model(monkeypatch, ["not json at all", GOOD])
    with TestClient(app) as client:
        sid = buy(client, member_id, taken=BAR_TAKEN)
        d = dispute(client, "bar", sid).json()["dispute"]
        assert d["outcome"] == "refunded" and len(calls) == 2
        assert review_of(d["dispute_id"])["attempts"] == 2


def test_model_without_image_support_falls_back_to_the_timeline(member_id, monkeypatch):
    calls = with_model(monkeypatch, [httpx.HTTPStatusError("400 image", request=None, response=httpx.Response(400)), GOOD])
    assert real_probe() is False  # the startup check: the tiny image was refused
    assert review.vision_ok is False and events("review_model_probe")[-1]["ok"] is False
    with TestClient(app) as client:
        sid = buy_bar_with_photos(client, member_id)
        d = dispute(client, "bar", sid).json()["dispute"]
        assert d["outcome"] == "refunded"
        assert calls[-1]["images"] == [] and "timeline only" in json.loads(calls[-1]["text"])["images"]
        assert review_of(d["dispute_id"])["source"] == "timeline"


def test_probe_with_image_support(monkeypatch):
    calls = with_model(monkeypatch, ["Red."])
    assert real_probe() is True and review.vision_ok is True
    assert len(calls[0]["images"]) == 1 and calls[0]["images"][0][:2] == b"\xff\xd8"
    assert events("review_model_probe")[-1]["ok"] is True


def test_probe_without_a_model_does_nothing(monkeypatch):
    assert real_probe() is None and review.vision_ok is None
    assert events("review_model_probe")[-1]["reason"] == "no_model"


# --- the admin decision flow ---

def test_admin_approves_the_refund_with_a_note(member_id, monkeypatch):
    with_model(monkeypatch, [LEANS])
    with TestClient(app) as client:
        sid = buy(client, member_id)
        points = points_of(member_id)
        d = dispute(client, "elx", sid).json()["dispute"]
        login_as(client, member_id, admin=True)
        assert client.post(f"/admin/disputes/{d['dispute_id']}/approve", json={"note": ""}).json()["error"] == "note_required"
        r = client.post(f"/admin/disputes/{d['dispute_id']}/approve", json={"note": "Photos show it never left."})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["dispute"]["outcome"] == "refunded" and body["dispute"]["amount_usd"] == 3.78
        assert body["refund"]["reason"] == "staff_review" and body["refund"]["dispute_id"] == d["dispute_id"]
        assert body["dispute"]["decision"]["by"] == "staff" and body["dispute"]["decision"]["agreed"] is True
        assert body["dispute"]["decision"]["note"] == "Photos show it never left."
        assert points_of(member_id) == points - 3
        logged = events("dispute_decision")[-1]
        assert (logged["by"], logged["decision"], logged["ai_verdict"], logged["agreed"]) == ("staff", "approve", "supports_customer", True)
        assert client.post(f"/admin/disputes/{d['dispute_id']}/approve", json={"note": "again"}).json()["error"] == "dispute_closed"
        m = client.get("/admin/state").json()["metrics"]
        assert (m["disputes_today"], m["ai_human_agreement_pct"], m["auto_approved_today"]) == (1, 100, 0)
        login_as(client, member_id)
        receipt = client.get(f"/api/receipt/{sid}").json()
        assert receipt["disputes"][0]["customer_status"] == {"label": "Refunded", "note": None}
        assert receipt["refunds"][0]["reason"] == "staff_review"


def test_admin_keeps_the_charge_with_a_note(member_id, monkeypatch):
    with_model(monkeypatch, [LEANS])
    with TestClient(app) as client:
        sid = buy(client, member_id)
        d = dispute(client, "elx", sid).json()["dispute"]
        login_as(client, member_id, admin=True)
        r = client.post(f"/admin/disputes/{d['dispute_id']}/keep", json={"note": "The drink left the shelf at 10:14."})
        assert r.status_code == 200, r.text
        out = r.json()["dispute"]
        assert out["outcome"] == "charge_confirmed" and out["status"] == "resolved" and out["refund_id"] is None
        assert out["decision"]["agreed"] is False  # the AI supported the customer, the person disagreed
        assert out["message"] == "Our team reviewed the shelf photos and confirmed the charge."
        assert refund_rows() == []
        m = client.get("/admin/state").json()["metrics"]
        assert m["ai_human_agreement_pct"] == 0
        login_as(client, member_id)
        receipt = client.get(f"/api/receipt/{sid}").json()
        assert receipt["disputes"][0]["customer_status"] == {"label": "Charge confirmed",
                                                             "note": "The drink left the shelf at 10:14."}
        # settled: the shopper can neither keep nor remove it any more
        assert client.post(f"/api/disputes/{d['dispute_id']}/remove").json()["error"] == "dispute_closed"


def test_agreement_is_unknown_when_the_review_was_unclear(member_id):
    with TestClient(app) as client:
        sid = buy(client, member_id)
        d = dispute(client, "elx", sid).json()["dispute"]  # no model: unclear
        login_as(client, member_id, admin=True)
        out = client.post(f"/admin/disputes/{d['dispute_id']}/keep", json={"note": "Checked the shelf myself."}).json()
        assert out["dispute"]["decision"]["agreed"] is None
        assert client.get("/admin/state").json()["metrics"]["ai_human_agreement_pct"] is None


def test_admin_approval_while_shopping_takes_the_unit_off_the_cart(member_id, monkeypatch):
    with_model(monkeypatch, [LEANS])
    with TestClient(app) as client:
        enter(client, member_id)  # a hydration drink in the cart, $3.78, reviewed under 80 %: waits for a person
        d = dispute(client, "elx").json()["dispute"]
        assert d["status"] == "open"
        login_as(client, member_id, admin=True)
        body = client.post(f"/admin/disputes/{d['dispute_id']}/approve", json={"note": "Tag visible."}).json()
        assert body["dispute"]["outcome"] == "removed" and body["cart"]["items"] == []
        assert events("override")[-1]["source"] == "staff_review"
        # a staff removal does not use up the shopper's own "Remove anyway" taps
        assert body["dispute"]["remove_anyway_left"] == 2


def test_small_case_while_shopping_is_taken_off_the_cart_by_the_policy(member_id, monkeypatch):
    with_model(monkeypatch, [GOOD])
    with TestClient(app) as client:
        enter(client, member_id, taken=BAR_TAKEN)
        r = dispute(client, "bar").json()
        assert r["dispute"]["outcome"] == "removed" and r["cart"]["items"] == []
        assert events("override")[-1]["source"] == "ai_auto_small"
        assert r["dispute"]["message"] == "Removed from your cart after a review of the shelf photos."


def test_decisions_and_reviews_are_pushed_to_admins_and_the_shopper(member_id, monkeypatch):
    with_model(monkeypatch, [LEANS])  # supports the customer under 80 %: stays open, "Under review"
    pushed = []
    from backend import ws
    monkeypatch.setattr(ws.manager, "publish", lambda msg, **kw: pushed.append((msg["type"], kw, msg["data"])))
    with TestClient(app) as client:
        sid = buy(client, member_id)
        d = dispute(client, "elx", sid).json()["dispute"]
        kinds = [(t, "admin" if kw.get("admin") else kw.get("member_id")) for t, kw, _ in pushed if t == "dispute"]
        assert ("dispute", "admin") in kinds and ("dispute", member_id) in kinds
        admin_msgs = [data for t, kw, data in pushed if t == "dispute" and kw.get("admin")]
        assert admin_msgs[-1]["review"]["verdict"] == "supports_customer" and admin_msgs[-1]["dispute_id"] == d["dispute_id"]
        shopper_msgs = [data for t, kw, data in pushed if t == "dispute" and kw.get("member_id") == member_id]
        assert "review" not in shopper_msgs[-1] and shopper_msgs[-1]["customer_status"]["label"] == "Under review"


def test_review_off_with_disputes_off(member_id, monkeypatch):
    set_features(monkeypatch, disputes=False)
    set_env(monkeypatch, anthropic_api_key="test-key")
    assert review.available() is False
