"""AI assisted dispute review with a human in the loop (8.14, F20 `disputes`).

When a dispute reaches needs_review (while shopping, at the exit, or "Report a problem" after paying), a vision
capable model looks at the keyframes of the disputed bay around the relevant changes, the baseline and latest
crops, and a structured timeline (tag ids seen per frame window, motion periods, stable changes, cart changes),
and answers strict JSON: how strongly the evidence supports the shopper, a verdict, a summary of at most 40 words
and observations tied to frame ids. The reply is validated here: the verdict is "unclear" whenever the
percentage is between UNCLEAR_LOW and UNCLEAR_HIGH, words like cheat, fraud or lying are replaced, unknown
frame ids are dropped. One call of REVIEW_TIMEOUT_S with one retry; after that the review is "unclear" with the
summary "AI review unavailable".

The policy is code, not the model: a review that supports the customer at AUTO_APPROVE_MIN_PCT or more for a
disputed amount under AUTO_APPROVE_MAX_CENTS is approved at once through the existing refund path (reason
"ai_auto_small"). Everything else waits for a person on the admin page, who approves the refund or keeps the
charge with a short note. The AI can never deny a dispute: there is no code path from a review to "keep the
charge". Every decision is logged with the AI verdict and whether the person agreed with it.

Provider and model come from REVIEW_PROVIDER / REVIEW_MODEL in .env, defaulting to the F11 LLM settings (the
OpenAI compatible base URL and key are reused). probe() runs at startup: a tiny image goes to the model; if it
is refused, reviews use the timeline only and the console says so.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend import admin, db, eventlog, evidence, store, ws
from backend import cart as cart_mod
from backend.routes_api import ApiError
from backend.settings import settings

log = logging.getLogger("backend.review")

REVIEW_TIMEOUT_S = 20.0
RETRIES = 1
AUTO_APPROVE_MAX_CENTS = 500  # under $5.00
AUTO_APPROVE_MIN_PCT = 80
UNCLEAR_LOW, UNCLEAR_HIGH = 35, 65  # inclusive band that is always "unclear"
MAX_SUMMARY_WORDS = 40
MAX_OBSERVATION_WORDS = 40
MAX_EVIDENCE = 8
MAX_IMAGES = 8  # baseline + latest + up to 6 keyframes
MAX_TOKENS = 500
UNAVAILABLE_SUMMARY = "AI review unavailable"
NEUTRAL_WORD = "discrepancy"
NOTE_MIN, NOTE_MAX = 3, 300

SUPPORTS_CUSTOMER, SUPPORTS_CHARGE, UNCLEAR = "supports_customer", "supports_charge", "unclear"
VERDICTS = (SUPPORTS_CUSTOMER, SUPPORTS_CHARGE, UNCLEAR)
AI, STAFF = "ai", "staff"
APPROVE, KEEP = "approve", "keep"
AI_AUTO_SMALL, STAFF_REVIEW = "ai_auto_small", "staff_review"  # refund reasons (8.11)

# Words that must never reach a shopper or an admin from the model. Replaced, never shown.
BANNED_RE = re.compile(
    r"\b(cheat\w*|fraud\w*|lying|lies|lied|liar\w*|steal\w*|stole\w*|stolen|theft\w*|thie(?:f|ves)\w*|"
    r"shoplift\w*|dishonest\w*|scam\w*|deceiv\w*|deceit\w*)\b", re.IGNORECASE)

SYSTEM_PROMPT = (
    "You review shelf camera evidence for a small smart store. A shopper says one unit of an item was put in "
    "their cart or on their receipt but they did not take it. You get shelf photos (crops of one bay or of the "
    "shelf, never people's faces) with frame ids, and a timeline: unit tag ids seen in each photo, motion "
    "periods, stable changes and cart changes. The missing unit is identified by its tag id. When the products "
    "carry no tags (timeline detection \"yolo\"), photos and changes carry the number of units of each item the "
    "detector counted instead, and the missing unit is the count of that item that dropped.\n"
    "Judge how strongly the evidence supports the shopper's account (the unit stayed on the shelf, was put back, "
    "moved to another bay, or was never clearly taken) versus the charge (the unit clearly left the shelf during "
    "the visit and did not come back).\n"
    "Reply with strict JSON only, no prose, no code fences:\n"
    '{"supports_customer_pct": 0 to 100, "verdict": "supports_customer" | "supports_charge" | "unclear", '
    '"summary": at most 40 words, "evidence": [{"frame": frame id, "observation": short text}]}\n'
    "Rules: the verdict is \"unclear\" whenever the percentage is between 35 and 65. Be neutral and factual: "
    "describe what the frames show, never judge the person, and never use words like cheat, fraud, steal or "
    "lying. Use only the frame ids you were given. At most 6 evidence items."
)

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
PROBE_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABALDA4MChAODQ4SERATGCgaGBYWGDEjJR0oOjM9PDkzODdASFxOQERXRTc4UG1RV19iZ2hnPk1x"
    "eXBkeFxlZ2P/2wBDARESEhgVGC8aGi9jQjhCY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2P/"
    "wAARCAAIAAgDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQR"
    "BRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1"
    "dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6"
    "/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKR"
    "obHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOU"
    "lZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDAoooo"
    "rzj7Q/9k=")

router = APIRouter(dependencies=[Depends(admin.require_admin)])

vision_ok: bool | None = None  # None until probe() ran (or no model is configured)
probe_result: dict[str, Any] | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --- provider ---

def provider() -> str:
    env = settings.env
    return (env.review_provider or env.llm_provider or "anthropic").lower()


def model_name() -> str:
    env = settings.env
    if env.review_model:
        return env.review_model
    return env.openai_model if provider() == "openai" else env.anthropic_model


def api_key() -> str:
    env = settings.env
    return env.openai_api_key if provider() == "openai" else env.anthropic_api_key


def available() -> bool:
    """A model is configured and the flags allow it. Off means every review is "AI review unavailable"."""
    return bool(settings.features.llm and settings.features.disputes and api_key() and model_name())


def call_model(system: str, text: str, images: list[bytes], timeout: float = REVIEW_TIMEOUT_S) -> str:
    """One raw reply from the review model, with `images` (JPEG bytes) attached before the text.
    Raises on any HTTP, timeout or shape problem; tests replace this function."""
    env = settings.env
    b64 = [base64.b64encode(img).decode() for img in images]
    if provider() == "openai":
        content: list[dict[str, Any]] = [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}"}}
                                         for b in b64]
        content.append({"type": "text", "text": text})
        r = httpx.post(
            f"{env.openai_base_url.rstrip('/')}/chat/completions",
            headers={"authorization": f"Bearer {env.openai_api_key}", "content-type": "application/json"},
            json={"model": model_name(), "max_tokens": MAX_TOKENS,
                  "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}]},
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b}} for b in b64]
    content.append({"type": "text", "text": text})
    r = httpx.post(
        ANTHROPIC_URL,
        headers={"x-api-key": env.anthropic_api_key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": model_name(), "max_tokens": MAX_TOKENS, "system": system,
              "messages": [{"role": "user", "content": content}]},
        timeout=timeout,
    )
    r.raise_for_status()
    return "".join(block.get("text", "") for block in r.json()["content"] if block.get("type") == "text")


def probe() -> bool | None:
    """Startup check: can the review model take an image? Sets vision_ok and logs the answer clearly.
    None when no model is configured (nothing to probe)."""
    global vision_ok, probe_result
    if not available():
        vision_ok = None
        probe_result = {"ok": None, "provider": provider(), "model": model_name() or None, "reason": "no_model"}
        eventlog.log("review_model_probe", **probe_result)
        log.info("[review] no review model configured: disputes get \"AI review unavailable\" until a person decides")
        return None
    t0 = time.monotonic()
    try:
        reply = call_model("Answer with one word.", "What colour is this image?", [PROBE_JPEG], timeout=REVIEW_TIMEOUT_S)
        vision_ok = bool(str(reply).strip())
        error = None
    except Exception as e:  # HTTP 4xx (no image support), timeout, bad shape: text only from now on
        vision_ok = False
        error = repr(e)[:300]
    probe_result = {"ok": vision_ok, "provider": provider(), "model": model_name(),
                    "elapsed_ms": int((time.monotonic() - t0) * 1000), "error": error}
    eventlog.log("review_model_probe", **probe_result)
    if vision_ok:
        log.info("[review] %s/%s accepts images: disputes get a vision review", provider(), model_name())
    else:
        log.warning("[review] %s/%s did NOT accept an image (%s). Dispute reviews fall back to the timeline only; "
                    "set REVIEW_MODEL to a vision capable model in .env to fix this.", provider(), model_name(), error)
    return vision_ok


# --- inputs ---

def _parse_iso(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def cart_changes(session_id: str, limit: int = 2000) -> list[dict[str, Any]]:
    """This visit's cart_changed events from the log, oldest first (the tail of the log, not the whole file)."""
    out = []
    for e in eventlog.tail(limit):
        if e.get("type") == "cart_changed" and e.get("session_id") == session_id:
            out.append({"at": e.get("ts"), "items": e.get("items"), "total_usd": e.get("total_usd"),
                        "source": e.get("source")})
    return out


def disputed_cents(row: dict[str, Any]) -> int:
    """What a refund of the disputed unit is: its unit price (as paid, when the visit was paid) plus tax."""
    sku = row["sku"]
    session = store.get_session(row["store_session_id"])
    price = None
    if session and session.get("final_cart"):
        price = next((cart_mod.to_cents(i["unit_price_usd"]) for i in session["final_cart"].get("items", [])
                      if i["sku"] == sku), None)
    if price is None:
        price = cart_mod.to_cents(settings.skus[sku].price_usd) if sku in settings.skus else 0
    return price + cart_mod.tax_cents(price, settings.store["tax_rate"])


def gather(row: dict[str, Any]) -> dict[str, Any]:
    """The review's inputs: frames (id, path, caption) and the structured timeline for the disputed bay."""
    from backend import disputes  # local: disputes imports this module

    sku = row["sku"]
    session_id = row["store_session_id"]
    ev = json.loads(row["evidence_json"] or "{}") or {}
    bay = ev.get("bay") if ev.get("bay") is not None else disputes.home_bay(sku)
    tag_id = ev.get("tag_id")
    root = disputes.evidence_root() / session_id
    crops = disputes.crops(session_id, bay)
    clips = evidence.clips_for(session_id, bay)
    session = store.get_session(session_id) or {}

    frames: list[dict[str, Any]] = []
    baseline = next((c for c in crops if c["kind"] == "baseline"), crops[0] if crops else None)
    latest = crops[-1] if crops else None
    if baseline is not None and (root / baseline["file"]).is_file():
        frames.append({"id": "baseline", "path": str(root / baseline["file"]), "captured_at": baseline["captured_at"],
                       "caption": f"Bay {bay + 1} when the shopper walked in", "units": baseline["units"],
                       "yolo_counts": baseline.get("yolo_counts", {})})
    if latest is not None and latest is not baseline and (root / latest["file"]).is_file():
        frames.append({"id": "latest", "path": str(root / latest["file"]), "captured_at": latest["captured_at"],
                       "caption": f"Bay {bay + 1} now", "units": latest["units"],
                       "yolo_counts": latest.get("yolo_counts", {})})

    # The most relevant clip: the newest one in which the missing unit was there before the change (by tag id,
    # or without tags by the SKU's count dropping across the change).
    relevant = [c for c in clips if tag_id is not None and tag_id in c["units_before"]] \
        or [c for c in clips if c.get("counts_before", {}).get(sku, 0) > c.get("counts_after", {}).get(sku, 0)] \
        or clips
    chosen = relevant[-1] if relevant else None
    if chosen is not None:
        for k in chosen["keyframes"]:
            path = evidence.clips_dir(session_id) / k["file"]
            if len(frames) >= MAX_IMAGES:
                break
            if path.is_file():
                frames.append({"id": evidence.frame_id(chosen["id"], k["id"]), "path": str(path),
                               "captured_at": k["captured_at"],
                               "caption": f"Shelf around the change in bay {bay + 1} at {chosen['change_at']}"})

    # Tag free mode: no crop saw a tag but the crops carry YOLO counts; the timeline then speaks in counts.
    tag_free = bool(crops) and not any(c["tags"] or c["units"] for c in crops) \
        and any(c.get("yolo_counts") for c in crops)
    b_counts = baseline.get("yolo_counts", {}) if baseline else {}
    l_counts = latest.get("yolo_counts", {}) if latest else {}
    timeline = {
        "item": settings.skus[sku].name if sku in settings.skus else sku,
        "sku": sku,
        "bay_card": bay + 1,
        "detection": "yolo" if tag_free else "tags",
        "missing_tag_id": tag_id,
        "sku_tag_ids": sorted(t for t, u in settings.units.items() if u.sku == sku),
        "stage": row["stage"],
        "dispute_opened_at": row["created_at"],
        "session_started_at": session.get("started_at"),
        "baseline_units": baseline["units"] if baseline else None,
        "latest_units": latest["units"] if latest else None,
        "baseline_counts": b_counts if baseline else None,
        "latest_counts": l_counts if latest else None,
        "missing_count": max(0, b_counts.get(sku, 0) - l_counts.get(sku, 0)) if tag_free and baseline and latest else None,
        "tags_seen": [{"at": c["captured_at"], "kind": c["kind"], "tag_ids": sorted(int(t) for t in c["tags"]),
                       "yolo_counts": c.get("yolo_counts", {})} for c in crops],
        "stable_changes": [{"at": c["captured_at"], "units": c["units"], "yolo_counts": c.get("yolo_counts", {})}
                           for c in crops if c["kind"] == "change"],
        "clips": [{"clip_id": c["id"], "change_at": c["change_at"], "from": c["starts_at"], "to": c["ends_at"],
                   "units_before": c["units_before"], "units_after": c["units_after"],
                   "counts_before": c.get("counts_before", {}), "counts_after": c.get("counts_after", {}),
                   "motion_periods": [{"from": a, "to": b} for a, b in c["motion"]],
                   "keyframes": [{"id": evidence.frame_id(c["id"], k["id"]), "at": k["captured_at"]}
                                 for k in c["keyframes"]]} for c in clips],
        "cart_changes": cart_changes(session_id),
    }
    return {"frames": frames, "timeline": timeline, "disputed_cents": disputed_cents(row)}


def compose(inputs: dict[str, Any], with_images: bool) -> tuple[str, list[bytes]]:
    """(user text, image bytes in frame order). Without images the text says so and lists the frames anyway."""
    images: list[bytes] = []
    frames = []
    for f in inputs["frames"]:
        if with_images:
            try:
                images.append(Path(f["path"]).read_bytes())
            except OSError:
                continue
        frames.append({"id": f["id"], "caption": f["caption"], "captured_at": f["captured_at"],
                       **({"units": f["units"]} if "units" in f else {}),
                       **({"yolo_counts": f["yolo_counts"]} if f.get("yolo_counts") else {})})
    payload = {
        "task": "Review this cart dispute.",
        "images": ("The images are attached in the order listed under frames."
                   if images else "No images could be attached: judge from the timeline only."),
        "frames": frames,
        "timeline": inputs["timeline"],
    }
    return json.dumps(payload), images


# --- validation ---

def sanitize(text: Any) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return BANNED_RE.sub(NEUTRAL_WORD, text)


def _words(text: str, limit: int) -> str:
    words = text.split()
    return " ".join(words[:limit]) if len(words) > limit else text


def verdict_for(pct: int, given: Any = None) -> str:
    """The verdict the percentage allows: always "unclear" inside the band, else the one the percentage implies.
    The model's own verdict (`given`) is only logged; a verdict that contradicts its percentage never gets out."""
    if UNCLEAR_LOW <= pct <= UNCLEAR_HIGH:
        return UNCLEAR
    return SUPPORTS_CUSTOMER if pct > UNCLEAR_HIGH else SUPPORTS_CHARGE


def _extract_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.MULTILINE).strip()
    try:
        obj = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object in the reply")
        obj = json.loads(text[start:end + 1])
    if not isinstance(obj, dict):
        raise ValueError("reply is not a JSON object")
    return obj


def validate(raw: Any, frame_ids: set[str] | None = None) -> dict[str, Any]:
    """The review in its strict shape, or ValueError. Percent clamped to 0..100, verdict forced by the band,
    banned words replaced, summary and observations capped, evidence limited to known frame ids."""
    obj = _extract_json(raw)
    try:
        pct = int(round(float(obj["supports_customer_pct"])))
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError("supports_customer_pct missing or not a number") from e
    pct = max(0, min(100, pct))
    verdict = verdict_for(pct, obj.get("verdict"))
    summary = _words(sanitize(obj.get("summary")), MAX_SUMMARY_WORDS) or "No summary given."
    items = obj.get("evidence") or []
    evidence_out = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            fid = str(item.get("frame") or "").strip()
            if frame_ids is not None and fid not in frame_ids:
                continue
            observation = _words(sanitize(item.get("observation")), MAX_OBSERVATION_WORDS)
            if not observation:
                continue
            evidence_out.append({"frame": fid, "observation": observation})
            if len(evidence_out) >= MAX_EVIDENCE:
                break
    return {"supports_customer_pct": pct, "verdict": verdict, "summary": summary, "evidence": evidence_out}


def unavailable(reason: str | None = None) -> dict[str, Any]:
    return {"supports_customer_pct": 50, "verdict": UNCLEAR, "summary": UNAVAILABLE_SUMMARY, "evidence": [],
            "source": "unavailable", "model": model_name() or None, "error": reason}


def review_dispute(row: dict[str, Any], inputs: dict[str, Any] | None = None) -> dict[str, Any]:
    """Ask the model (REVIEW_TIMEOUT_S, one retry) and validate. Never raises: on failure the review is
    "unclear" with "AI review unavailable"."""
    if not available():
        return unavailable("no_model")
    inputs = inputs or gather(row)
    with_images = vision_ok is not False and bool(inputs["frames"])
    text, images = compose(inputs, with_images)
    frame_ids = {f["id"] for f in inputs["frames"]}
    last_error = None
    for attempt in range(1, RETRIES + 2):
        t0 = time.monotonic()
        try:
            raw = call_model(SYSTEM_PROMPT, text, images, REVIEW_TIMEOUT_S)
            result = validate(raw, frame_ids)
        except Exception as e:  # timeout, HTTP error, bad JSON
            last_error = repr(e)[:300]
            eventlog.log("review_attempt_failed", dispute_id=row["id"], attempt=attempt, error=last_error)
            continue
        result.update(source="vision" if images else "timeline", model=model_name(), attempts=attempt,
                      elapsed_ms=int((time.monotonic() - t0) * 1000), frames=[f["id"] for f in inputs["frames"]])
        return result
    return unavailable(last_error or "unknown")


# --- policy (code, not the model) ---

def policy(review: dict[str, Any], cents: int) -> str:
    """"auto_approve" only for a clear, small case; "human" for everything else. Never "deny"."""
    if review.get("verdict") == SUPPORTS_CUSTOMER and int(review.get("supports_customer_pct", 0)) >= AUTO_APPROVE_MIN_PCT \
            and cents < AUTO_APPROVE_MAX_CENTS:
        return "auto_approve"
    return "human"


def agreed(review: dict[str, Any] | None, decision: str) -> bool | None:
    """Did a staff decision agree with the AI verdict? None when there is no usable verdict."""
    verdict = (review or {}).get("verdict")
    if verdict == SUPPORTS_CUSTOMER:
        return decision == APPROVE
    if verdict == SUPPORTS_CHARGE:
        return decision == KEEP
    return None


# --- storage + delivery ---

def _store(dispute_id: str, column: str, value: dict[str, Any]) -> None:
    conn = db.connect()
    try:
        conn.execute(f"UPDATE disputes SET {column} = ? WHERE id = ?", (json.dumps(value), dispute_id))
        conn.commit()
    finally:
        conn.close()


def publish(row: dict[str, Any]) -> None:
    """{"type":"dispute"} (8.4): the admin view to admin sockets, the shopper view to the shopper."""
    from backend import disputes

    ws.manager.publish({"type": "dispute", "data": disputes.view(row, url_prefix="/admin/evidence", admin=True)},
                       admin=True)
    ws.manager.publish({"type": "dispute", "data": disputes.view(row)}, member_id=row["member_id"])


def run(dispute_id: str) -> dict[str, Any] | None:
    """Review one dispute, store the result, apply the policy, tell the admin page. Returns the review."""
    from backend import disputes

    row = disputes._row(dispute_id)
    if row is None:
        return None
    review = review_dispute(row)
    review["created_at"] = _now()
    _store(dispute_id, "review_json", review)
    eventlog.log("dispute_reviewed", dispute_id=dispute_id, session_id=row["store_session_id"], sku=row["sku"],
                 verdict=review["verdict"], supports_customer_pct=review["supports_customer_pct"],
                 source=review.get("source"), model=review.get("model"), summary=review["summary"],
                 evidence=len(review["evidence"]))
    row = disputes._row(dispute_id)
    cents = disputed_cents(row)
    decision = policy(review, cents)
    eventlog.log("dispute_policy", dispute_id=dispute_id, decision=decision, disputed_usd=cart_mod.to_usd(cents),
                 verdict=review["verdict"], supports_customer_pct=review["supports_customer_pct"])
    if decision == "auto_approve" and row["status"] == disputes.OPEN:
        try:
            approve(dispute_id, by=AI, reason=AI_AUTO_SMALL, note=None)
            return review
        except ApiError as e:  # nothing to give back any more, or the shopper settled it meanwhile
            eventlog.log("dispute_auto_approve_skipped", dispute_id=dispute_id, error=e.code, message=e.message)
    publish(disputes._row(dispute_id))
    return review


def _safe_run(dispute_id: str) -> None:
    try:
        run(dispute_id)
    except Exception as e:  # never let a review error surface anywhere but the log
        eventlog.log("review_error", dispute_id=dispute_id, error=repr(e)[:300])


def schedule(dispute_id: str) -> None:
    """Review in the background. With no model configured the "unavailable" review is stored at once."""
    if not available():
        _safe_run(dispute_id)
        return
    threading.Thread(target=_safe_run, args=(dispute_id,), name=f"review-{dispute_id}", daemon=True).start()


# --- decisions: the policy's auto approval and the admin buttons ---

def _decide(row: dict[str, Any], by: str, decision: str, note: str | None, reason: str | None) -> dict[str, Any]:
    review = json.loads(row["review_json"]) if row.get("review_json") else None
    record = {"by": by, "decision": decision, "note": note, "reason": reason, "at": _now(),
              "ai_verdict": (review or {}).get("verdict"),
              "ai_pct": (review or {}).get("supports_customer_pct"),
              "agreed": agreed(review, decision) if by == STAFF else None}
    _store(row["id"], "decision_json", record)
    eventlog.log("dispute_decision", dispute_id=row["id"], session_id=row["store_session_id"], sku=row["sku"],
                 by=by, decision=decision, reason=reason, note=note, ai_verdict=record["ai_verdict"],
                 ai_pct=record["ai_pct"], agreed=record["agreed"])
    return record


def approve(dispute_id: str, by: str, note: str | None, reason: str) -> dict[str, Any]:
    """Settle an open dispute in the shopper's favour through the existing paths: a refund of the unit plus tax
    when the visit was paid (reason `reason` on the Refund), else one unit off the live or frozen cart.
    409 dispute_closed, 409 nothing_to_refund when the visit ended without a charge."""
    from backend import disputes, members, payments

    with disputes._lock:
        row = disputes._row(dispute_id)
        if row is None:
            raise ApiError(404, "not_found", "No dispute here.")
        if row["status"] != disputes.OPEN:
            raise ApiError(409, "dispute_closed", "This dispute is already settled.")
        member = members.get_member(row["member_id"]) or {"id": row["member_id"]}
        session = store.get_session(row["store_session_id"])
        refund = cart = None
        paid = session is not None and session["state"] in (store.PAID, store.CLOSED) and not session.get("return_of")
        if paid:
            session, payment = disputes._paid_visit(row["member_id"], row["store_session_id"], row["sku"], window=False)
            refund, result = disputes._refund_one(member, session, payment, row["sku"], dispute_id, reason=reason)
            if refund["status"] != payments.REFUND_SUCCEEDED:
                eventlog.log("dispute_refund_failed", dispute_id=dispute_id, session_id=session["id"],
                             message=result.get("message"), by=by)
                raise ApiError(502, "refund_failed", result.get("message") or "The refund did not go through.")
            row = disputes._settle(dispute_id, disputes.REFUNDED, disputes.RESOLVED,
                                   cart_mod.to_cents(refund["amount_usd"]), refund["refund_id"])
            eventlog.log("dispute_refunded", dispute_id=dispute_id, session_id=session["id"], sku=row["sku"],
                         source=reason, amount_usd=refund["amount_usd"], refund_id=refund["refund_id"])
        else:
            active = store.current_session()
            if active is None or active["id"] != row["store_session_id"] or active["state"] not in store.SHOPPING_STATES \
                    or disputes._qty(store.cart_for(active), row["sku"]) < 1:
                raise ApiError(409, "nothing_to_refund",
                               "That item is no longer in the cart and this visit was not charged. Nothing to give back.")
            cart, amount = disputes._remove_from_cart(active, row["sku"], source=reason)
            row = disputes._settle(dispute_id, disputes.REMOVED, disputes.RESOLVED, amount)
            eventlog.log("dispute_removed", dispute_id=dispute_id, session_id=active["id"], sku=row["sku"],
                         source=reason, amount_usd=cart_mod.to_usd(amount), total_usd=cart["total_usd"])
        _decide(row, by, APPROVE, note, reason)
        row = disputes._row(dispute_id)
    publish(row)
    out: dict[str, Any] = {"dispute": disputes.view(row, url_prefix="/admin/evidence", refund=refund, admin=True)}
    if refund is not None:
        out["refund"] = refund
    if cart is not None:
        out["cart"] = cart
    return out


def keep_charge(dispute_id: str, note: str) -> dict[str, Any]:
    """A person keeps the charge: outcome charge_confirmed. Only staff can do this; the AI never can."""
    from backend import disputes

    with disputes._lock:
        row = disputes._row(dispute_id)
        if row is None:
            raise ApiError(404, "not_found", "No dispute here.")
        if row["status"] != disputes.OPEN:
            raise ApiError(409, "dispute_closed", "This dispute is already settled.")
        row = disputes._settle(dispute_id, disputes.CHARGE_CONFIRMED, disputes.RESOLVED)
        _decide(row, STAFF, KEEP, note, STAFF_REVIEW)
        row = disputes._row(dispute_id)
    eventlog.log("dispute_charge_confirmed", dispute_id=dispute_id, session_id=row["store_session_id"], sku=row["sku"])
    publish(row)
    return {"dispute": disputes.view(row, url_prefix="/admin/evidence", admin=True)}


class DecisionBody(BaseModel):
    note: str = ""


def _note(body: DecisionBody) -> str:
    note = re.sub(r"\s+", " ", body.note or "").strip()
    if len(note) < NOTE_MIN:
        raise ApiError(422, "note_required", "Add a short note for the record (a few words).")
    return note[:NOTE_MAX]


@router.post("/admin/disputes/{dispute_id}/approve")
def admin_approve(dispute_id: str, body: DecisionBody):
    """"Approve refund" with a note: the refund (or cart correction) through the existing paths."""
    return approve(dispute_id, by=STAFF, note=_note(body), reason=STAFF_REVIEW)


@router.post("/admin/disputes/{dispute_id}/keep")
def admin_keep(dispute_id: str, body: DecisionBody):
    """"Keep the charge" with a note. The shopper's receipt shows "Charge confirmed" and the note."""
    return keep_charge(dispute_id, _note(body))


# --- metrics for the admin card ---

def metrics(since: str) -> dict[str, Any]:
    """{"disputes_today", "ai_human_agreement_pct", "auto_approved_today", "reviewed_today", "open_now"}."""
    conn = db.connect()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT status, review_json, decision_json FROM disputes WHERE created_at >= ?", (since,))]
        open_now = conn.execute("SELECT COUNT(*) FROM disputes WHERE status = 'open'").fetchone()[0]
    finally:
        conn.close()
    decisions = [json.loads(r["decision_json"]) for r in rows if r.get("decision_json")]
    staff = [d for d in decisions if d.get("by") == STAFF and d.get("agreed") is not None]
    agreement = round(100 * sum(1 for d in staff if d["agreed"]) / len(staff)) if staff else None
    return {
        "disputes_today": len(rows),
        "reviewed_today": sum(1 for r in rows if r.get("review_json")),
        "ai_human_agreement_pct": agreement,
        "auto_approved_today": sum(1 for d in decisions if d.get("by") == AI),
        "open_now": open_now,
    }
