# AI assisted dispute review with a human in the loop (F20, spec 8.14)

Built 26 Sep 2026 in one unattended session. Section 8 changes were pre-approved and are documented in the spec
(8.1, 8.2, 8.3, 8.4, 8.11, 8.13 and the new 8.14), plus Sections 4, 5.1, 6, 11.5, 11.6, 15 and 17.

## What was built

| Piece | Where | Notes |
|---|---|---|
| Ring buffer + event clips | `vision/clips.py`, wired in `vision/worker.py` | 10 s at 5 fps, union of the bay ROIs + 24 px margin (even sized, never the full frame). On every stable change: MP4 (`mp4v`) from 4 s before to 2 s after + 6 keyframes under `data/evidence/<sid>/clips/`, posted to `POST /internal/clips`. A second change inside the 2 s extends the same clip. |
| Clip registration, lookups, files | `backend/evidence.py` (new), `clips` table in `backend/db.py` | Same lifetime as the crops: `disputes.cleanup_evidence()` deletes the folder and both tables' rows. |
| Review agent | `backend/review.py` (new) | Vision model via `REVIEW_PROVIDER` / `REVIEW_MODEL` (default: the F11 provider/key/base URL/model). Startup probe with a tiny JPEG; refusal → timeline only review, logged loudly. Strict JSON validated in code: pct clamped, `unclear` forced for 35–65, banned words → "discrepancy", unknown frame ids dropped, 40 word cap. 20 s timeout, one retry, then "AI review unavailable". |
| Policy | `review.policy` | `supports_customer` + pct ≥ 80 + disputed amount < $5.00 → auto approve through the existing refund path (reason `ai_auto_small`; while shopping: one unit off the cart, override source `ai_auto_small`). Everything else waits for a person. No code path lets the AI deny. |
| Admin queue | `web/admin.html`, `web/js/pages/admin.js`, `web/css/app.css`, routes in `review.py` | Live `{"type":"dispute"}` messages, WebAudio chime + badge + title count, AI verdict with confidence bar and summary, observations linked to keyframes, inline `<video>` per clip, baseline vs latest, timeline, "Approve refund" / "Keep the charge" with a required note. Results card: disputes today, AI · human agreement, auto approved. Health badge "Review AI". |
| Shopper receipt | `web/receipt.html`, `web/js/pages/receipt.js`, `GET /api/receipt` `disputes` | "Reported problems" card: Under review / Refunded / Removed from your cart / Charge confirmed (+ staff note), keyframe thumbnails, live refresh on the `dispute` socket message. |
| Privacy | `web/index.html`, clip routes | Signup line added. MP4 only for admins; keyframes for admins and the visit's own shopper. |
| Tests | `tests/test_clips.py`, `tests/test_review.py` | Ring buffer and boundaries with synthetic frames, retention, JSON validation, banned words, unclear band, policy boundaries ($4.99 vs $5.00, 79 vs 80), AI never denies, admin decision flow, timeout fallback, probe. Model mocked everywhere. |

`pytest -q`: 468 passed.

## Decisions taken without asking

- **Where "register it with backend/evidence.py" lives.** That file did not exist; crop registration is in
  `backend/disputes.py`. Clips got the new `backend/evidence.py` (registration, lookups, serving, deletion) and
  the token check moved there; `disputes.py` imports it. Crops stayed where they were.
- **Auto approve while shopping.** The brief says "auto approve the refund through the existing refund path".
  Before payment there is no refund, so the shopping/checkout stage uses the existing cart correction
  (`store.remove_one`, override source `ai_auto_small`) and the same policy thresholds. After paying it is the
  refund path with reason `ai_auto_small`.
- **Verdict outside the band follows the percentage.** A model reply with `pct: 100, verdict: supports_charge`
  cannot get out; the percentage wins. Inside 35–65 it is always `unclear`.
- **Staff note visibility.** The shopper sees the note only with "Charge confirmed" (the answer they are owed).
  Approval notes are for the record (admin page + event log).
- **Staff / policy settlements do not use up the shopper's two "Remove anyway" taps** (`remove_anyway_used`
  counts rows without a `decision_json`).
- **The 30 minute report window limits when a problem may be reported, not when staff may settle it**
  (`_paid_visit(window=False)` for decisions).
- **New outcome `charge_confirmed`** (status `resolved`) for "Keep the charge"; `kept` stays the shopper's own
  withdrawal.
- **Startup probe runs off the event loop** (`asyncio.to_thread`) so a slow model never delays uvicorn; `vision`
  is `null` in `health.review` until it finishes.
- **Ring buffer memory:** the union crop for the 5 bay layout is about 1224×468×3 bytes × 50 frames ≈ 86 MB.
  Fine on the demo laptop; lower `RING_SECONDS` or `CLIP_FPS` in `vision/clips.py` if the worker ever swaps.
- **Event clips are written for every stable change, not only picks** (put backs too): a dispute needs the
  whole story of the bay.

## Morning checklist

- [ ] `.env`: set `REVIEW_PROVIDER` / `REVIEW_MODEL` to a vision capable model (or leave empty to reuse the F11
      settings; an Anthropic model works as is). Start the backend and look for `review_model_probe` in the
      console / event log: `ok: true` means images are accepted; `false` means timeline only reviews.
- [ ] `admin.html` health badge shows **Review AI: Vision · <model>**.
- [ ] Physical check: start the worker, enter as Demo Shopper, pick an item, wait 3 s: `data/evidence/<sid>/clips/`
      holds `clip_bay<N>_<stamp>.mp4` + 6 `_k*.jpg`; the admin event log shows `clip_saved`. Open the MP4 in the
      admin card (after opening a dispute) and confirm it plays in the browser (Chrome/Edge play `mp4v`; if a
      browser refuses it, the keyframes still show).
- [ ] Physical check: dispute the item on the phone, leave the sheet open, confirm the laptop chimes (first
      chime needs one click on the page after loading, browsers block audio before a gesture) and the badge count
      appears; confirm the card shows a verdict within about 20 s.
- [ ] Decide once with **Keep the charge** and once with **Approve refund**; confirm the receipt shows
      "Charge confirmed" with the note, then "Refunded", and that `ai_human_agreement_pct` on the Results card
      updates.
- [ ] Dispute a protein bar ($3.78) with the AI clearly supporting the shopper (put the bar back before
      disputing does that; or watch a real clip): the card says "Auto approved by policy".
- [ ] Practise the 20 s **Dispute review** moment in `docs/DEMO.md` (step 10) and the two new Q&A answers.
- [ ] Optional: `python -m vision.worker` memory with the ring buffer (about 90 MB more than before).
