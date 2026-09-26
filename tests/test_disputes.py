"""Cart disputes with camera evidence (8.13, F20): the camera recheck, "Remove anyway", the limit of two,
evidence served only to its owner, evidence cleanup, the refund after paying, and the worker's bay crops.
No camera: fake snapshots, and small real JPEGs written where the vision worker would write them."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend import db, disputes, payments, returns, store
from backend.main import app
from identity_helpers import login_as, set_features
from test_cart import FULL, events, make_member, member_id, qty, tmp_data, with_bays  # noqa: F401
from test_returns import ELX_AND_BAR_TAKEN, ELX_TAKEN, ENTRY, EXIT, HEADERS, buy, face_id, points_of, refund_rows, shelf
from vision.evidence import EvidenceRecorder, file_name

pytestmark = pytest.mark.usefixtures("tmp_data")

STAFF = "Please ask a staff member."
PRIVACY = "These photos show only the shelf and are deleted after your visit."
BAY0_TAGS = {"0": [20.0, 40.0, 60.0, 60.0], "1": [120.0, 40.0, 60.0, 60.0]}


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    set_features(monkeypatch, gates=True, passkeys=True, stripe=False, loyalty=True, disputes=True)
    from backend import admin
    monkeypatch.setattr(admin, "force_decline", False)


def enter(client: TestClient, mid: str, taken=ELX_TAKEN) -> str:
    shelf(client, FULL, 1)
    face_id(client, mid)
    r = client.post("/api/gate/enter", json=ENTRY)
    assert r.status_code == 200, r.text
    shelf(client, taken, 2)
    return r.json()["session"]["id"]


def crop(client: TestClient, sid: str, bay: int = 0, kind: str = "baseline", units=(0, 1), tags=None,
         ts: int | None = None):
    """Write a 220x420 JPEG where the worker would, then announce it like the worker does."""
    ts = ts or int(time.time() * 1000)
    name = file_name(bay, ts, kind)
    folder = disputes.evidence_root() / sid
    folder.mkdir(parents=True, exist_ok=True)
    ok, jpg = cv2.imencode(".jpg", np.full((420, 220, 3), 128, np.uint8))
    (folder / name).write_bytes(jpg.tobytes())
    body = {"session_id": sid, "bay": bay, "kind": kind, "file": name, "ts": ts, "width": 220, "height": 420,
            "units": list(units), "tags": BAY0_TAGS if tags is None else tags}
    return client.post("/internal/evidence", json=body, headers=HEADERS), name


def with_photos(client: TestClient, sid: str) -> None:
    """Baseline of bay 0 with both hydration drinks, then tag 0 gone."""
    assert crop(client, sid, kind="baseline", units=(0, 1), ts=1_800_000_000_000)[0].status_code == 200
    assert crop(client, sid, kind="change", units=(1,), tags={"1": BAY0_TAGS["1"]},
                ts=1_800_000_005_000)[0].status_code == 200


def dispute(client: TestClient, sku: str, session_id: str | None = None):
    body = {"sku": sku, **({"session_id": session_id} if session_id else {})}
    return client.post("/api/disputes", json=body)


def dispute_rows() -> list[dict]:
    conn = db.connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM disputes ORDER BY rowid")]
    finally:
        conn.close()


# --- a) the camera sees the unit back: correct at once ---

def test_camera_resolves_dispute_when_the_unit_is_back_at_the_exit(member_id):
    with TestClient(app) as client:
        enter(client, member_id)
        quote = client.post("/api/gate/exit/quote", json=EXIT).json()
        assert quote["cart"]["total_usd"] == 3.78 and quote["cart"]["state"] == "CHECKOUT_PENDING"
        shelf(client, FULL, 3)  # put back after the cart froze: the frozen cart still charges it
        assert qty(client.get("/api/store/current").json()["cart"], "elx") == 1

        r = dispute(client, "elx")
        assert r.status_code == 200, r.text
        d = r.json()["dispute"]
        assert d["outcome"] == "resolved_camera" and d["status"] == "resolved"
        assert d["message"] == "Our mistake, removed from your cart."
        assert d["stage"] == "checkout" and d["amount_usd"] == 3.78
        assert r.json()["cart"]["items"] == [] and r.json()["cart"]["total_usd"] == 0.0
        # the exit asks again: the corrected frozen cart is empty, so nothing to pay
        again = client.post("/api/gate/exit/quote", json=EXIT).json()
        assert again["cart"]["state"] == "CLOSED" and again["instruction"] is None
        assert [e["outcome"] for e in events("dispute_opened")] == ["resolved_camera"]


def test_camera_resolves_a_manual_override_the_shelf_disagrees_with(member_id):
    with TestClient(app) as client:
        enter(client, member_id, taken=FULL)
        store.apply_override("bar", 1)  # staff added chips that are still on the shelf
        assert qty(client.get("/api/store/current").json()["cart"], "bar") == 1
        d = dispute(client, "bar").json()
        assert d["dispute"]["outcome"] == "resolved_camera" and d["cart"]["items"] == []
        assert events("override")[-1]["source"] == "dispute"


# --- b) still missing: needs_review with the photos ---

def test_needs_review_when_the_unit_is_still_missing(member_id):
    with TestClient(app) as client:
        sid = enter(client, member_id)
        with_photos(client, sid)
        r = dispute(client, "elx")
        assert r.status_code == 200, r.text
        d = r.json()["dispute"]
        assert d["outcome"] == "needs_review" and d["status"] == "open"
        assert qty(r.json()["cart"], "elx") == 1  # nothing changes until the shopper decides
        ev = d["evidence"]
        assert ev["bay"] == 0 and ev["card"] == 1 and ev["tag_id"] == 0 and ev["privacy"] == PRIVACY
        assert ev["before"]["url"].startswith(f"/api/disputes/evidence/{sid}/bay0_") and "baseline" in ev["before"]["url"]
        assert "change" in ev["now"]["url"]
        # the missing unit (tag 0) outlined where it was last seen, as fractions of the crop
        assert ev["before"]["outline"] == {"x": round(20 / 220, 4), "y": round(40 / 420, 4),
                                           "w": round(60 / 220, 4), "h": round(60 / 420, 4)}
        assert d["remove_anyway_left"] == 2
        # the dispute record keeps the evidence paths
        stored = json.loads(dispute_rows()[0]["evidence_json"])
        assert stored["before"]["path"] == f"data/evidence/{sid}/" + ev["before"]["url"].rsplit("/", 1)[1]


def test_found_it_keep_it_changes_nothing(member_id):
    with TestClient(app) as client:
        enter(client, member_id)
        d = dispute(client, "elx").json()["dispute"]
        r = client.post(f"/api/disputes/{d['dispute_id']}/keep")
        assert r.status_code == 200 and r.json()["dispute"]["outcome"] == "kept"
        assert qty(r.json()["cart"], "elx") == 1
        assert client.post(f"/api/disputes/{d['dispute_id']}/remove").json()["error"] == "dispute_closed"


def test_not_in_cart_and_unknown_items_are_refused(member_id):
    with TestClient(app) as client:
        enter(client, member_id)
        assert dispute(client, "bar").json()["error"] == "not_in_cart"
        assert dispute(client, "nope").json()["error"] == "unknown_sku"


# --- Remove anyway ---

def test_remove_anyway_updates_the_cart_and_totals(member_id):
    with TestClient(app) as client:
        sid = enter(client, member_id, taken=ELX_AND_BAR_TAKEN)
        with_photos(client, sid)
        assert client.get("/api/store/current").json()["cart"]["total_usd"] == 6.48
        d = dispute(client, "elx").json()["dispute"]
        r = client.post(f"/api/disputes/{d['dispute_id']}/remove")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["dispute"]["outcome"] == "removed" and body["dispute"]["amount_usd"] == 3.78
        assert body["dispute"]["message"] == "Removed. Our team will review the shelf photos."
        cart = body["cart"]
        assert qty(cart, "elx") == 0 and qty(cart, "bar") == 1
        assert (cart["subtotal_usd"], cart["tax_usd"], cart["total_usd"]) == (2.50, 0.20, 2.70)
        assert client.get("/api/store/current").json()["cart"]["total_usd"] == 2.70
        override = events("override")[-1]
        assert override["source"] == "dispute" and override["delta"] == -1 and override["sku"] == "elx"
        assert events("dispute_removed")[-1]["dispute_id"] == d["dispute_id"]
        # the charge uses the corrected cart
        quote = client.post("/api/gate/exit/quote", json=EXIT).json()
        assert quote["cart"]["total_usd"] == 2.70 and quote["instruction"]["amount_usd"] == 2.70


def test_remove_anyway_at_the_exit_changes_the_frozen_cart(member_id):
    with TestClient(app) as client:
        enter(client, member_id, taken=ELX_AND_BAR_TAKEN)
        assert client.post("/api/gate/exit/quote", json=EXIT).json()["instruction"]["amount_usd"] == 6.48
        d = dispute(client, "bar").json()["dispute"]
        assert d["outcome"] == "needs_review" and d["stage"] == "checkout"
        body = client.post(f"/api/disputes/{d['dispute_id']}/remove").json()
        assert body["cart"]["state"] == "CHECKOUT_PENDING" and body["cart"]["total_usd"] == 3.78
        quote = client.post("/api/gate/exit/quote", json=EXIT).json()
        assert quote["instruction"]["amount_usd"] == 3.78
        face_id(client, member_id)
        paid = client.post("/api/gate/exit/approve").json()["payment"]
        assert paid["status"] == "AUTHORIZED" and paid["amount_usd"] == 3.78


def test_remove_anyway_limit_is_two_per_visit(member_id):
    with TestClient(app) as client:
        enter(client, member_id, taken=with_bays(b0=[], b2=[5]))  # 2 hydration drinks + 1 chips
        for _ in range(2):
            d = dispute(client, "elx").json()["dispute"]
            assert d["outcome"] == "needs_review"
            assert client.post(f"/api/disputes/{d['dispute_id']}/remove").status_code == 200
        d = dispute(client, "bar").json()["dispute"]
        assert d["remove_anyway_left"] == 0
        r = client.post(f"/api/disputes/{d['dispute_id']}/remove")
        assert r.status_code == 409 and r.json() == {"error": "dispute_limit", "message": STAFF}
        assert qty(client.get("/api/store/current").json()["cart"], "bar") == 1  # still in the cart
        assert events("dispute_limit")[-1]["limit"] == 2
        # every dispute is logged
        assert len(events("dispute_opened")) == 3 and len(events("dispute_removed")) == 2


# --- evidence: only the owner sees it, and it goes away ---

def test_evidence_is_served_only_to_the_session_owner(member_id):
    with TestClient(app) as client:
        sid = enter(client, member_id)
        r, name = crop(client, sid)
        assert r.status_code == 200
        url = f"/api/disputes/evidence/{sid}/{name}"
        got = client.get(url)
        assert got.status_code == 200 and got.headers["content-type"] == "image/jpeg"
        assert got.content[:2] == b"\xff\xd8"
        assert client.get(f"/api/disputes/evidence/{sid}/bay0_nope.jpg").status_code == 404
        assert client.get(f"/api/disputes/evidence/{sid}/..%2F..%2Fspeedmart.db").status_code == 404

        login_as(client, make_member("Sam"))
        assert client.get(url).status_code == 404  # someone else's visit
        client.cookies.clear()
        assert client.get(url).status_code == 401  # signed out
        login_as(client, member_id, admin=True)
        assert client.get(f"/admin/evidence/{sid}/{name}").status_code == 200  # the team's disputes card
        login_as(client, member_id)
        assert client.get(f"/admin/evidence/{sid}/{name}").status_code == 401


def test_evidence_notice_needs_the_token_and_a_shopping_session(member_id):
    with TestClient(app) as client:
        sid = enter(client, member_id)
        assert client.post("/internal/shelf", json={"ts": 1, "frame_id": 9, "bays": []},
                           headers=HEADERS).json()["session_id"] == sid
        r = client.post("/internal/evidence", json={"session_id": sid, "bay": 0, "kind": "baseline",
                                                     "file": "bay0_x.jpg", "ts": 1, "width": 1, "height": 1})
        assert r.status_code == 401
        store.cancel(sid)
        assert crop(client, sid)[0].status_code == 409  # the worker then deletes its file
        assert client.post("/internal/shelf", json={"ts": 1, "frame_id": 10, "bays": []},
                           headers=HEADERS).json()["session_id"] is None


def test_evidence_cleanup(member_id):
    with TestClient(app) as client:
        # a cancelled visit without a dispute: deleted
        sid = enter(client, member_id)
        crop(client, sid)
        assert disputes.cleanup_evidence() == []  # still shopping: kept
        store.cancel(sid)
        assert disputes.cleanup_evidence() == [sid]
        assert not (disputes.evidence_root() / sid).exists() and disputes.crops(sid, 0) == []
        assert events("evidence_deleted")[-1] == {**events("evidence_deleted")[-1], "session_id": sid, "files": 1}

        # a visit with an open dispute: kept 24 hours after the dispute
        sid2 = enter(client, member_id)
        with_photos(client, sid2)
        dispute(client, "elx")
        store.cancel(sid2)
        assert disputes.cleanup_evidence() == []
        assert len(list((disputes.evidence_root() / sid2).glob("*.jpg"))) == 2
        later = datetime.now(timezone.utc) + timedelta(hours=24, minutes=1)
        assert disputes.cleanup_evidence(later) == [sid2]

        # a paid visit: kept while "Report a problem" is open (30 min), then deleted
        shelf(client, FULL, 20)
        face_id(client, member_id)
        sid3 = client.post("/api/gate/enter", json=ENTRY).json()["session"]["id"]
        crop(client, sid3)
        shelf(client, ELX_TAKEN, 21)
        client.post("/api/gate/exit/quote", json=EXIT)
        face_id(client, member_id)
        assert client.post("/api/gate/exit/approve").json()["payment"]["status"] == "AUTHORIZED"
        assert disputes.cleanup_evidence() == []
        assert disputes.cleanup_evidence(datetime.now(timezone.utc) + timedelta(minutes=31)) == [sid3]


# --- after paying: "Report a problem" refunds through the returns refund path ---

def test_post_purchase_dispute_refund_reuses_the_refund_path(member_id, monkeypatch):
    calls = []
    real_refund = payments.refund

    def spy(*args, **kwargs):
        calls.append(kwargs.get("reason"))
        return real_refund(*args, **kwargs)

    monkeypatch.setattr(payments, "refund", spy)
    with TestClient(app) as client:
        sid = buy(client, member_id)
        points = points_of(member_id)
        report = client.get(f"/api/receipt/{sid}").json()["report"]
        assert report["eligible"] and report["items"] == [{"sku": "elx", "name": "Hydration drink", "qty": 1}]

        d = dispute(client, "elx", sid).json()["dispute"]
        assert d["outcome"] == "needs_review" and d["stage"] == "after_purchase"  # the shelf still has it gone
        r = client.post(f"/api/disputes/{d['dispute_id']}/remove")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["dispute"]["outcome"] == "refunded" and body["dispute"]["amount_usd"] == 3.78
        assert body["dispute"]["message"] == "Refunded $3.78. Our team will review the shelf photos."
        assert body["refund"]["reason"] == "dispute" and body["refund"]["dispute_id"] == d["dispute_id"]
        assert body["refund"]["return_session_id"] is None and body["refund"]["status"] == "SUCCEEDED"
        assert calls == ["dispute"]
        row = refund_rows()[-1]
        assert (row["reason"], row["dispute_id"], row["amount_cents"]) == ("dispute", d["dispute_id"], 378)
        assert points_of(member_id) == points - 3
        receipt = client.get(f"/api/receipt/{sid}").json()
        assert receipt["refunded_usd"] == 3.78 and receipt["refunds"][0]["reason"] == "dispute"
        assert receipt["report"]["eligible"] is False and receipt["return"]["reason"] == "nothing_to_return"
        # the same unit can never be refunded twice, by a dispute or a return
        assert dispute(client, "elx", sid).json()["error"] == "nothing_to_refund"


def test_post_purchase_camera_refunds_when_the_unit_is_on_the_shelf(member_id):
    with TestClient(app) as client:
        sid = buy(client, member_id)
        shelf(client, FULL, 5)  # the hydration drinks are on the shelf: the camera got the purchase wrong
        body = dispute(client, "elx", sid).json()
        assert body["dispute"]["outcome"] == "resolved_camera" and body["refund"]["amount_usd"] == 3.78
        assert body["dispute"]["message"] == "Our mistake. Refund of $3.78 is on its way to your Visa ending 4242."


def test_post_purchase_dispute_rules(member_id):
    with TestClient(app) as client:
        sid = buy(client, member_id)
        assert dispute(client, "bar", sid).json()["error"] == "nothing_to_refund"
        login_as(client, make_member("Sam"))
        assert dispute(client, "elx", sid).status_code == 404
        login_as(client, member_id)
        conn = db.connect()
        try:
            old = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat(timespec="seconds").replace("+00:00", "Z")
            conn.execute("UPDATE payments SET created_at = ?", (old,))
            conn.commit()
        finally:
            conn.close()
        r = dispute(client, "elx", sid)
        assert r.status_code == 409 and r.json()["error"] == "dispute_window_closed"
        assert client.get(f"/api/receipt/{sid}").json()["report"]["eligible"] is False


def test_admin_state_lists_disputes_with_photos(member_id):
    with TestClient(app) as client:
        sid = enter(client, member_id)
        with_photos(client, sid)
        dispute(client, "elx")
        login_as(client, member_id, admin=True)
        listed = client.get("/admin/state").json()["disputes"]
        assert len(listed) == 1 and listed[0]["member_name"] == "Demo Shopper"
        assert listed[0]["evidence"]["before"]["url"].startswith(f"/admin/evidence/{sid}/")
        assert listed[0]["outcome"] == "needs_review"


def test_disputes_off_never_breaks_the_core_loop(member_id, monkeypatch):
    set_features(monkeypatch, disputes=False)
    with TestClient(app) as client:
        sid = enter(client, member_id)
        assert dispute(client, "elx").json()["error"] == "disputes_off"
        assert client.post("/internal/shelf", json={"ts": 1, "frame_id": 9, "bays": []},
                           headers=HEADERS).json()["session_id"] is None
        assert crop(client, sid)[0].json()["error"] == "disputes_off"
        client.post("/api/gate/exit/quote", json=EXIT)
        face_id(client, member_id)
        assert client.post("/api/gate/exit/approve").json()["payment"]["status"] == "AUTHORIZED"
        assert client.get(f"/api/receipt/{sid}").json()["report"]["eligible"] is False


# --- the vision worker's crops ---

class Status:
    def __init__(self, stable: bool, units=()):
        self.stable, self.units = stable, tuple(units)


class Det:
    def __init__(self, tag_id, x, y, size=40):
        self.tag_id, self.center = tag_id, (x + size / 2, y + size / 2)
        self.corners = np.array([[x, y], [x + size, y], [x + size, y + size], [x, y + size]], dtype=np.float32)


def test_worker_saves_bay_crops_only_for_the_shopping_session(tmp_path):
    bays = [{"id": 0, "roi": [40, 200, 260, 620]}, {"id": 1, "roi": [285, 200, 505, 620]}]
    rec = EvidenceRecorder(bays, tmp_path, "http://x/internal/evidence", "t", start=False)
    frame = np.zeros((720, 1280, 3), np.uint8)
    both = {0: Status(True, [0, 1]), 1: Status(True, [2])}
    assert rec.update(frame, both, []) == []  # nobody shopping: nothing saved

    rec.set_session("ses_abc")
    first = rec.update(frame, {0: Status(True, [0, 1]), 1: Status(False, [2])}, [Det(0, 60, 250)], ts_ms=1000)
    assert [(c.bay, c.kind) for c in first] == [(0, "baseline")]  # bay 1 has a hand in it: wait
    assert first[0].image.shape == (420, 220, 3)  # the bay's ROI, never the full frame
    assert first[0].tags == {"0": [20.0, 50.0, 40.0, 40.0]}
    assert [(c.bay, c.kind) for c in rec.update(frame, both, [], ts_ms=1100)] == [(1, "baseline")]
    assert rec.update(frame, both, [], ts_ms=1200) == []  # nothing changed
    changed = rec.update(frame, {0: Status(True, [1]), 1: Status(True, [2])}, [], ts_ms=1300)
    assert [(c.bay, c.kind, c.units) for c in changed] == [(0, "change", [1])]

    path = rec.save(changed[0])
    assert path.parent == tmp_path / "ses_abc" and disputes.FILE_RE.match(path.name)
    assert cv2.imread(str(path)).shape == (420, 220, 3)
    notice = rec.notice(changed[0], path)
    assert notice["session_id"] == "ses_abc" and notice["kind"] == "change" and (notice["width"], notice["height"]) == (220, 420)

    rec.set_session(None)
    assert rec.update(frame, {0: Status(True, [])}, []) == []  # session over: no more crops
