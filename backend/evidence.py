"""Event clips for dispute review (8.14, F20 `disputes`): registration, lookups and files.

The vision worker (vision/clips.py) writes, for every stable change in a bay during a shopping session, an MP4 of
the union of the bay ROIs from 4 s before to 2 s after the change plus 6 JPEG keyframes, under
data/evidence/<session_id>/clips/, and announces each one on POST /internal/clips (X-Internal-Token). Rows live in
the `clips` table (Section 6). The bay crops themselves (baseline / change photos) stay in backend/disputes.py;
both share the same lifetime: disputes.cleanup_evidence() deletes the folder and both tables' rows.

Files are served to admins (MP4 and keyframes, /admin/evidence/<sid>/clips/<file>) and to the visit's own
shopper (keyframes only, /api/disputes/evidence/<sid>/clips/<file>); the routes are in disputes.py.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend import db, eventlog, store
from backend.routes_api import ApiError
from backend.settings import settings

SESSION_RE = re.compile(r"^ses_[A-Za-z0-9_-]{1,40}$")
CLIP_RE = re.compile(r"^clip_bay\d{1,3}_[A-Za-z0-9-]{1,64}\.mp4$")
KEYFRAME_RE = re.compile(r"^clip_bay\d{1,3}_[A-Za-z0-9-]{1,64}_k\d{1,2}\.jpg$")
MAX_KEYFRAMES = 12

router = APIRouter()


def evidence_root() -> Path:
    return db.DATA_DIR / "evidence"


def clips_dir(session_id: str) -> Path:
    return evidence_root() / session_id / "clips"


def check_internal_token(request: Request) -> None:
    token = request.headers.get("X-Internal-Token", "")
    expected = settings.env.internal_token
    if not expected or not secrets.compare_digest(token.encode(), expected.encode()):
        raise ApiError(401, "bad_internal_token", "Missing or wrong X-Internal-Token.")


def iso_ms(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --- POST /internal/clips (8.2) ---

class Keyframe(BaseModel):
    id: str
    file: str
    ts: int


class ClipNotice(BaseModel):
    session_id: str
    bay: int
    sku: str
    file: str
    keyframes: list[Keyframe]
    change_ts: int
    starts_ts: int
    ends_ts: int
    units_before: list[int] = []
    units_after: list[int] = []
    # Tag free mode (vision.mode "yolo"), optional: the bay's stable YOLO counts before and after the change.
    counts_before: dict[str, int] = {}
    counts_after: dict[str, int] = {}
    motion: list[list[int]] = []  # [[start_ms, end_ms], ...]
    width: int
    height: int
    fps: float = 5.0
    frames: int = 0


@router.post("/internal/clips")
def clip_notice(body: ClipNotice, request: Request):
    """The worker saved one event clip for the shopping session: remember it for the review and the admin queue.
    404 disputes_off / file_missing, 409 session_not_active (the worker then deletes the files), 422 bad names."""
    check_internal_token(request)
    if not settings.features.disputes:
        raise ApiError(404, "disputes_off", "Disputes are off for this demo.")
    if not SESSION_RE.match(body.session_id) or not CLIP_RE.match(body.file) \
            or not body.keyframes or len(body.keyframes) > MAX_KEYFRAMES \
            or any(not KEYFRAME_RE.match(k.file) or not re.match(r"^k\d{1,2}$", k.id) for k in body.keyframes):
        raise ApiError(422, "bad_clip", "Unexpected session id or file name.")
    if body.bay not in {b.id for b in settings.bays} or body.sku not in settings.skus:
        raise ApiError(422, "bad_clip", f"Unknown bay {body.bay} or SKU {body.sku}.")
    session = store.get_session(body.session_id)
    if session is None or session["state"] not in store.SHOPPING_STATES:
        raise ApiError(409, "session_not_active", "That session is not shopping any more.")
    folder = clips_dir(body.session_id)
    if not (folder / body.file).is_file() or any(not (folder / k.file).is_file() for k in body.keyframes):
        raise ApiError(404, "file_missing", "The clip is not on disk.")
    keyframes = [{"id": k.id, "file": k.file, "captured_at": iso_ms(k.ts)} for k in body.keyframes]
    motion = [[iso_ms(int(a)), iso_ms(int(b))] for a, b in (m[:2] for m in body.motion if len(m) >= 2)]
    counts_before = {k: int(v) for k, v in body.counts_before.items() if k in settings.skus and int(v) > 0}
    counts_after = {k: int(v) for k, v in body.counts_after.items() if k in settings.skus and int(v) > 0}
    conn = db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO clips (store_session_id, bay, sku, file, keyframes_json, change_at, starts_at, ends_at, "
            "units_before_json, units_after_json, counts_before_json, counts_after_json, motion_json, width, "
            "height, fps, frames, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (body.session_id, body.bay, body.sku, body.file, json.dumps(keyframes), iso_ms(body.change_ts),
             iso_ms(body.starts_ts), iso_ms(body.ends_ts), json.dumps(sorted(set(body.units_before))),
             json.dumps(sorted(set(body.units_after))), json.dumps(counts_before), json.dumps(counts_after),
             json.dumps(motion), body.width, body.height, body.fps, body.frames or len(keyframes), db.now_iso()))
        clip_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    eventlog.log("clip_saved", session_id=body.session_id, clip_id=clip_id, bay=body.bay, sku=body.sku,
                 file=body.file, keyframes=len(keyframes), units_before=sorted(set(body.units_before)),
                 units_after=sorted(set(body.units_after)), counts_before=counts_before, counts_after=counts_after,
                 motion_periods=len(motion))
    return {"ok": True, "clip_id": clip_id}


# --- lookups ---

def _parse_row(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["keyframes"] = json.loads(d.pop("keyframes_json"))
    d["units_before"] = json.loads(d.pop("units_before_json"))
    d["units_after"] = json.loads(d.pop("units_after_json"))
    d["counts_before"] = json.loads(d.pop("counts_before_json", None) or "{}")
    d["counts_after"] = json.loads(d.pop("counts_after_json", None) or "{}")
    d["motion"] = json.loads(d.pop("motion_json"))
    return d


def clips_for(session_id: str, bay: int | None = None) -> list[dict[str, Any]]:
    """This visit's clips (of one bay when given), oldest first."""
    conn = db.connect()
    try:
        if bay is None:
            rows = conn.execute("SELECT * FROM clips WHERE store_session_id = ? ORDER BY change_at, id",
                                (session_id,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM clips WHERE store_session_id = ? AND bay = ? ORDER BY change_at, id",
                                (session_id, bay)).fetchall()
        return [_parse_row(r) for r in rows]
    finally:
        conn.close()


def frame_id(clip_id: int, key_id: str) -> str:
    """The id the review agent refers to a keyframe by: clip<id>_k<n>."""
    return f"clip{int(clip_id)}_{key_id}"


def clip_view(row: dict[str, Any], url_prefix: str, with_video: bool) -> dict[str, Any]:
    """A clip for the API: keyframe urls always, the MP4 url only for admins (`with_video`)."""
    folder = clips_dir(row["store_session_id"])
    base = f"{url_prefix}/{row['store_session_id']}/clips"
    return {
        "clip_id": row["id"],
        "bay": row["bay"], "card": row["bay"] + 1, "sku": row["sku"],
        "name": settings.skus[row["sku"]].name if row["sku"] in settings.skus else row["sku"],
        "url": f"{base}/{row['file']}" if with_video and (folder / row["file"]).is_file() else None,
        "keyframes": [{"id": frame_id(row["id"], k["id"]), "url": f"{base}/{k['file']}", "captured_at": k["captured_at"]}
                      for k in row["keyframes"] if (folder / k["file"]).is_file()],
        "change_at": row["change_at"], "starts_at": row["starts_at"], "ends_at": row["ends_at"],
        "units_before": row["units_before"], "units_after": row["units_after"],
        "counts_before": row.get("counts_before", {}), "counts_after": row.get("counts_after", {}),
        "motion": [{"from": a, "to": b} for a, b in row["motion"]],
        "fps": row["fps"], "frames": row["frames"], "width": row["width"], "height": row["height"],
    }


def keyframe_path(row: dict[str, Any], fid: str) -> Path | None:
    """Disk path of the keyframe with review frame id `fid` in this clip, if it exists."""
    for k in row["keyframes"]:
        if frame_id(row["id"], k["id"]) == fid:
            path = clips_dir(row["store_session_id"]) / k["file"]
            return path if path.is_file() else None
    return None


def serve(session_id: str, file: str, keyframes_only: bool) -> FileResponse:
    """A registered clip file. 404 for unknown names, unregistered files, and the MP4 when keyframes_only."""
    is_key = bool(KEYFRAME_RE.match(file))
    is_clip = bool(CLIP_RE.match(file))
    if not (is_key or is_clip) or (is_clip and keyframes_only):
        raise ApiError(404, "not_found", "No clip here.")
    conn = db.connect()
    try:
        rows = conn.execute("SELECT file, keyframes_json FROM clips WHERE store_session_id = ?", (session_id,)).fetchall()
    finally:
        conn.close()
    known = any(r["file"] == file or any(k["file"] == file for k in json.loads(r["keyframes_json"])) for r in rows)
    path = clips_dir(session_id) / file
    if not known or not path.is_file():
        raise ApiError(404, "not_found", "No clip here.")
    return FileResponse(path, media_type="video/mp4" if is_clip else "image/jpeg",
                        headers={"Cache-Control": "private, max-age=600"})


def session_ids() -> set[str]:
    conn = db.connect()
    try:
        return {r[0] for r in conn.execute("SELECT DISTINCT store_session_id FROM clips")}
    finally:
        conn.close()


def delete_for(session_id: str) -> int:
    """Forget a visit's clips (the files go with the evidence folder). Returns the row count."""
    conn = db.connect()
    try:
        n = conn.execute("DELETE FROM clips WHERE store_session_id = ?", (session_id,)).rowcount
        conn.commit()
        return n
    finally:
        conn.close()
