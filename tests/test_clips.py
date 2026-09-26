"""Event clips for dispute review (8.14): the worker's ring buffer and clip boundaries with synthetic frames, the
MP4 + keyframes on disk, the /internal/clips notice, who may fetch what, and retention. No camera, no model."""

from __future__ import annotations

import time

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend import db, disputes, evidence, store
from backend.main import app
from identity_helpers import login_as, set_features
from test_cart import FULL, events, make_member, member_id, qty, tmp_data, with_bays  # noqa: F401
from test_disputes import crop, dispute, enter
from test_returns import EXIT, HEADERS, buy, face_id, shelf
from vision.clips import (ClipRecorder, FrameRing, clip_name, clips_url, keyframe_indexes, keyframe_name,
                          motion_periods, union_box)

pytestmark = pytest.mark.usefixtures("tmp_data")

BAYS = [{"id": 0, "roi": [40, 200, 260, 620], "sku": "elx"}, {"id": 1, "roi": [285, 200, 505, 620], "sku": "rec"}]


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    set_features(monkeypatch, gates=True, passkeys=True, stripe=False, loyalty=True, disputes=True)
    from backend import admin
    monkeypatch.setattr(admin, "force_decline", False)


class Status:
    def __init__(self, stable: bool, units=(), motion: bool = False):
        self.stable, self.units, self.motion = stable, tuple(units), motion


class Change:
    """What EvidenceRecorder.update hands over for a saved crop."""

    def __init__(self, bay, kind, units, ts_ms, session_id="ses_abc"):
        self.bay, self.kind, self.units, self.ts_ms, self.session_id = bay, kind, list(units), ts_ms, session_id


def frame(shade: int) -> np.ndarray:
    return np.full((720, 1280, 3), shade, np.uint8)


# --- pure helpers ---

def test_union_box_covers_every_bay_plus_margin_and_clamps():
    assert union_box([b["roi"] for b in BAYS], (720, 1280, 3), margin=24) == (16, 176, 528, 644)  # even width
    assert union_box([[0, 0, 100, 100], [1200, 700, 1400, 800]], (720, 1280, 3), margin=10) == (0, 0, 1280, 720)
    assert union_box([], (720, 1280, 3)) == (0, 0, 1280, 720)
    assert union_box([[0, 0, 101, 51]], (720, 1280, 3), margin=0) == (0, 0, 100, 50)


def test_keyframe_indexes_are_evenly_spaced_with_first_and_last():
    assert keyframe_indexes(31) == [0, 6, 12, 18, 24, 30]
    assert keyframe_indexes(6) == [0, 1, 2, 3, 4, 5]
    assert keyframe_indexes(4) == [0, 1, 2, 3]
    assert keyframe_indexes(1) == [0]
    assert keyframe_indexes(0) == []


def test_ring_samples_at_5_fps_and_keeps_10_seconds():
    ring = FrameRing(fps=5, seconds=10)
    kept = [ring.push(ts, frame(0)) for ts in range(0, 1000, 33)]  # a 30 fps camera for one second
    assert sum(kept) == 5 and len(ring) == 5  # 0, 200, 400, 600, 800 (33 ms steps: 198 is too early, 231 is kept)
    for ts in range(1000, 20000, 200):
        ring.push(ts, frame(0))
    assert len(ring) == 50  # 10 s at 5 fps, the oldest dropped
    assert ring.newest_ts == 19800
    assert [s.ts_ms for s in ring.window(15000, 15400)] == [15000, 15200, 15400]


def test_motion_periods_are_runs_of_moving_samples():
    ring = FrameRing(fps=5, seconds=10)
    moving = {1000, 1200, 1400, 2200}
    for ts in range(0, 3000, 200):
        ring.push(ts, frame(0), {0: ts in moving, 1: False})
    assert motion_periods(ring.window(0, 3000), 0, 200) == [(1000, 1600), (2200, 2400)]
    assert motion_periods(ring.window(0, 3000), 1, 200) == []


def test_names_and_url():
    assert clip_name(0, 1_800_000_000_123) == "clip_bay0_20270115T080000123Z.mp4"
    assert keyframe_name(0, 1_800_000_000_123, 3) == "clip_bay0_20270115T080000123Z_k3.jpg"
    assert evidence.CLIP_RE.match(clip_name(2, 1_800_000_000_000)) and evidence.KEYFRAME_RE.match(keyframe_name(2, 1, 0))
    assert clips_url("http://127.0.0.1:8000/internal/shelf") == "http://127.0.0.1:8000/internal/clips"


# --- the recorder: boundaries, files, notice ---

def test_clip_runs_from_4s_before_to_2s_after_the_change(tmp_path):
    rec = ClipRecorder(BAYS, tmp_path, "http://x/internal/clips", "t", start=False)
    stable = {0: Status(True, [0, 1]), 1: Status(True, [2])}
    # nobody shopping for the first 3 s: frames still enter the ring, nothing is recorded
    for ts in range(0, 3000, 200):
        assert rec.update(frame(ts // 100), stable, [], ts) == []
    rec.set_session("ses_abc")
    assert rec.update(frame(30), stable, [Change(0, "baseline", [0, 1], 3000)], 3000) == []  # baseline: no clip
    for ts in range(3200, 9000, 200):
        assert rec.update(frame(ts // 100), stable, [], ts) == []
    # the change is confirmed at t = 10 s; a hand moved over the bay from 9.0 s to 9.6 s
    moving = {0: Status(True, [1], motion=True), 1: Status(True, [2])}
    for ts in (9000, 9200, 9400):
        rec.update(frame(ts // 100), moving, [], ts)
    rec.update(frame(96), stable, [], 9600)
    rec.update(frame(98), stable, [], 9800)
    done = rec.update(frame(100), {0: Status(True, [1]), 1: Status(True, [2])}, [Change(0, "change", [1], 10000)], 10000)
    assert done == []  # not finished: it still needs the 2 s after the change
    for ts in range(10200, 12000, 200):
        assert rec.update(frame(ts // 100), stable, [], ts) == []
    done = rec.update(frame(120), stable, [], 12000)
    assert len(done) == 1
    clip = done[0]
    assert (clip.session_id, clip.bay, clip.sku, clip.change_ts) == ("ses_abc", 0, "elx", 10000)
    assert (clip.start_ts, clip.end_ts) == (6000, 12000)
    assert [s.ts_ms for s in clip.samples][:3] == [6000, 6200, 6400] and clip.samples[-1].ts_ms == 12000
    assert len(clip.samples) == 31  # 6 s at 5 fps, both ends included
    assert (clip.units_before, clip.units_after) == ([0, 1], [1])
    assert clip.motion == [(9000, 9600)]
    assert clip.size == (512, 468)  # the union of the bays plus the margin, never the full 1280x720
    # the ring never held a whole frame either
    assert all(s.image.shape == (468, 512, 3) for s in clip.samples)

    path, keys = rec.save(clip)
    assert path == tmp_path / "ses_abc" / "clips" / "clip_bay0_19700101T000010000Z.mp4"
    assert [k.name for k in keys] == [f"clip_bay0_19700101T000010000Z_k{i}.jpg" for i in range(6)]
    video = cv2.VideoCapture(str(path))
    assert video.isOpened() and int(video.get(cv2.CAP_PROP_FRAME_COUNT)) == 31
    assert (int(video.get(cv2.CAP_PROP_FRAME_WIDTH)), int(video.get(cv2.CAP_PROP_FRAME_HEIGHT))) == (512, 468)
    video.release()
    # the keyframes are the 1st, 7th, ... 31st frames: shades 60, 72, 84, 96, 108, 120
    assert [int(cv2.imread(str(k))[10, 10, 0]) // 2 * 2 for k in keys] == [60, 72, 84, 96, 108, 120]
    notice = rec.notice(clip, path, keys)
    assert notice["session_id"] == "ses_abc" and notice["file"] == path.name and notice["frames"] == 31
    assert [k["id"] for k in notice["keyframes"]] == ["k0", "k1", "k2", "k3", "k4", "k5"]
    assert [k["ts"] for k in notice["keyframes"]] == [6000, 7200, 8400, 9600, 10800, 12000]
    assert notice["motion"] == [[9000, 9600]] and (notice["width"], notice["height"]) == (512, 468)
    assert (notice["change_ts"], notice["starts_ts"], notice["ends_ts"]) == (10000, 6000, 12000)


def test_a_second_change_inside_the_window_extends_the_clip(tmp_path):
    rec = ClipRecorder(BAYS, tmp_path, "http://x/internal/clips", "t", start=False)
    rec.set_session("ses_abc")
    stable = {0: Status(True, [0, 1]), 1: Status(True, [2])}
    for ts in range(0, 5000, 200):
        rec.update(frame(0), stable, [], ts)
    rec.update(frame(0), stable, [Change(0, "change", [1], 5000)], 5000)
    rec.update(frame(0), stable, [Change(0, "change", [], 6000)], 6000)  # 1 s later the other unit goes too
    done = []
    for ts in range(6200, 9000, 200):
        done += rec.update(frame(0), stable, [], ts)
    assert len(done) == 1 and (done[0].change_ts, done[0].end_ts) == (5000, 8000)
    assert (done[0].units_before, done[0].units_after) == ([0, 1], [])


def test_no_clip_without_a_session_and_pending_clips_die_with_it(tmp_path):
    rec = ClipRecorder(BAYS, tmp_path, "http://x/internal/clips", "t", start=False)
    stable = {0: Status(True, [0, 1])}
    for ts in range(0, 3000, 200):
        rec.update(frame(0), stable, [], ts)
    assert rec.update(frame(0), stable, [Change(0, "change", [1], 3000)], 3000) == []
    rec.set_session("ses_abc")
    rec.update(frame(0), stable, [Change(0, "change", [1], 3200)], 3200)
    rec.set_session(None)  # paid: the backend would refuse the clip anyway
    assert all(rec.update(frame(0), stable, [], ts) == [] for ts in range(3400, 7000, 200))


def test_recorder_writes_and_posts_on_its_thread(tmp_path):
    sent = []
    rec = ClipRecorder(BAYS, tmp_path, "http://x/internal/clips", "t", send=lambda c, p, k: sent.append((p, k)))
    try:
        rec.set_session("ses_abc")
        stable = {0: Status(True, [0])}
        for ts in range(0, 5000, 200):
            rec.update(frame(0), stable, [], ts)
        rec.update(frame(0), stable, [Change(0, "change", [], 5000)], 5000)
        for ts in range(5200, 7400, 200):
            rec.update(frame(0), stable, [], ts)
        deadline = time.time() + 5
        while not sent and time.time() < deadline:
            time.sleep(0.05)
    finally:
        rec.stop()
    assert len(sent) == 1 and sent[0][0].is_file() and len(sent[0][1]) == 6 and all(k.is_file() for k in sent[0][1])


# --- the backend: registration, views, who may fetch what, retention ---

def write_clip(sid: str, bay: int = 0, change_ts: int = 1_800_000_010_000, n_keys: int = 6):
    folder = evidence.clips_dir(sid)
    folder.mkdir(parents=True, exist_ok=True)
    img = np.full((468, 512, 3), 90, np.uint8)
    mp4 = folder / clip_name(bay, change_ts)
    writer = cv2.VideoWriter(str(mp4), cv2.VideoWriter_fourcc(*"mp4v"), 5, (512, 468))
    for _ in range(3):
        writer.write(img)
    writer.release()
    keys = []
    for i in range(n_keys):
        k = folder / keyframe_name(bay, change_ts, i)
        cv2.imwrite(str(k), img)
        keys.append({"id": f"k{i}", "file": k.name, "ts": change_ts - 4000 + i * 1200})
    return mp4.name, keys


def clip_notice(client: TestClient, sid: str, bay: int = 0, sku: str = "elx", **over):
    name, keys = write_clip(sid, bay)
    body = {"session_id": sid, "bay": bay, "sku": sku, "file": name, "keyframes": keys,
            "change_ts": 1_800_000_010_000, "starts_ts": 1_800_000_006_000, "ends_ts": 1_800_000_012_000,
            "units_before": [0, 1], "units_after": [1], "motion": [[1_800_000_009_000, 1_800_000_009_600]],
            "width": 512, "height": 468, "fps": 5, "frames": 31, **over}
    return client.post("/internal/clips", json=body, headers=HEADERS), name, keys


def test_clip_notice_is_registered_and_listed_with_its_keyframes(member_id):
    with TestClient(app) as client:
        sid = enter(client, member_id)
        r, name, keys = clip_notice(client, sid)
        assert r.status_code == 200 and r.json() == {"ok": True, "clip_id": 1}
        rows = evidence.clips_for(sid, 0)
        assert len(rows) == 1 and rows[0]["units_before"] == [0, 1] and rows[0]["units_after"] == [1]
        assert rows[0]["motion"] == [["2027-01-15T08:00:09.000Z", "2027-01-15T08:00:09.600Z"]]
        assert rows[0]["change_at"] == "2027-01-15T08:00:10.000Z"
        assert [k["id"] for k in rows[0]["keyframes"]] == ["k0", "k1", "k2", "k3", "k4", "k5"]
        assert events("clip_saved")[-1]["keyframes"] == 6
        admin = evidence.clip_view(rows[0], "/admin/evidence", with_video=True)
        assert admin["url"] == f"/admin/evidence/{sid}/clips/{name}" and admin["card"] == 1
        assert admin["keyframes"][0] == {"id": "clip1_k0", "url": f"/admin/evidence/{sid}/clips/{keys[0]['file']}",
                                         "captured_at": "2027-01-15T08:00:06.000Z"}
        assert admin["motion"] == [{"from": "2027-01-15T08:00:09.000Z", "to": "2027-01-15T08:00:09.600Z"}]
        shopper = evidence.clip_view(rows[0], "/api/disputes/evidence", with_video=False)
        assert shopper["url"] is None and len(shopper["keyframes"]) == 6


def test_clip_notice_refusals(member_id):
    with TestClient(app) as client:
        sid = enter(client, member_id)
        body = clip_notice(client, sid)[0].request.content  # a good body, reused below
        import json
        good = json.loads(body)
        assert client.post("/internal/clips", json=good).status_code == 401  # no token
        assert client.post("/internal/clips", json={**good, "file": "../../x.mp4"}, headers=HEADERS).json()["error"] == "bad_clip"
        assert client.post("/internal/clips", json={**good, "bay": 9}, headers=HEADERS).json()["error"] == "bad_clip"
        assert client.post("/internal/clips", json={**good, "file": "clip_bay0_nope.mp4"}, headers=HEADERS).json()["error"] == "file_missing"
        store.cancel(sid)
        assert client.post("/internal/clips", json=good, headers=HEADERS).status_code == 409  # session over


def test_clips_off_with_disputes_off(member_id, monkeypatch):
    set_features(monkeypatch, disputes=False)
    with TestClient(app) as client:
        sid = enter(client, member_id)
        assert clip_notice(client, sid)[0].json()["error"] == "disputes_off"


def test_clip_files_go_to_admins_and_keyframes_to_the_owner_only(member_id):
    with TestClient(app) as client:
        sid = enter(client, member_id)
        _, name, keys = clip_notice(client, sid)
        mp4 = f"/api/disputes/evidence/{sid}/clips/{name}"
        key = f"/api/disputes/evidence/{sid}/clips/{keys[2]['file']}"
        got = client.get(key)
        assert got.status_code == 200 and got.headers["content-type"] == "image/jpeg"
        assert client.get(mp4).status_code == 404  # the shopper never gets the video
        assert client.get(f"/api/disputes/evidence/{sid}/clips/clip_bay0_x_k9.jpg").status_code == 404
        login_as(client, make_member("Sam"))
        assert client.get(key).status_code == 404
        client.cookies.clear()
        assert client.get(key).status_code == 401
        login_as(client, member_id, admin=True)
        video = client.get(f"/admin/evidence/{sid}/clips/{name}")
        assert video.status_code == 200 and video.headers["content-type"] == "video/mp4"
        assert client.get(f"/admin/evidence/{sid}/clips/{keys[0]['file']}").status_code == 200
        login_as(client, member_id)
        assert client.get(f"/admin/evidence/{sid}/clips/{name}").status_code == 401


def test_disputes_carry_the_bay_clips(member_id):
    with TestClient(app) as client:
        sid = enter(client, member_id)
        clip_notice(client, sid)
        d = dispute(client, "elx").json()["dispute"]
        assert d["outcome"] == "needs_review"
        assert len(d["clips"]) == 1 and d["clips"][0]["url"] is None and len(d["clips"][0]["keyframes"]) == 6
        assert d["clips"][0]["keyframes"][0]["url"].startswith(f"/api/disputes/evidence/{sid}/clips/")
        assert d["customer_status"] == {"label": "Under review", "note": None}
        login_as(client, member_id, admin=True)
        listed = client.get("/admin/state").json()["disputes"][0]
        assert listed["clips"][0]["url"].startswith(f"/admin/evidence/{sid}/clips/") and listed["first_name"] == "Demo"
        assert listed["timeline"]["clips"][0]["units_before"] == [0, 1]
        assert listed["timeline"]["clips"][0]["keyframes"][0]["id"] == "clip1_k0"
        assert listed["review"]["summary"] == "AI review unavailable"  # no model in tests: stored at once


def test_clips_share_the_evidence_retention(member_id):
    with TestClient(app) as client:
        sid = enter(client, member_id)
        crop(client, sid)
        clip_notice(client, sid)
        assert disputes.cleanup_evidence() == []  # shopping: kept
        store.cancel(sid)
        assert disputes.cleanup_evidence() == [sid]
        assert not evidence.clips_dir(sid).exists() and evidence.clips_for(sid) == [] and disputes.crops(sid, 0) == []
        deleted = events("evidence_deleted")[-1]
        assert (deleted["session_id"], deleted["files"], deleted["clip_files"], deleted["clips"]) == (sid, 1, 7, 1)

        # a paid visit with a dispute keeps its clips 24 h after the dispute, like the crops
        sid2 = buy(client, member_id)
        # the worker only saves clips while the visit is shopping; register one for it before paying next time
        assert evidence.clips_for(sid2) == []
        sid3 = enter(client, member_id)
        clip_notice(client, sid3)
        dispute(client, "elx")
        store.cancel(sid3)
        assert disputes.cleanup_evidence() == []
        assert evidence.clips_dir(sid3).is_dir()
