# Tag free YOLO mode (unattended session, 26 Sep 2026)

Scope: `config.json` (`vision.mode`), `backend/settings.py`, `backend/shelf_state.py`, `backend/cart.py`,
`backend/store.py`, `backend/db.py`, `backend/disputes.py`, `backend/evidence.py`, `backend/review.py`,
`vision/fusion.py`, `vision/worker.py`, `vision/overlay.py`, `vision/evidence.py`, `vision/clips.py`,
`training/capture.py`, `training/TRAINING.md`, `scripts/fake_shelf.py`, `scripts/e2e_sim.py`,
`web/js/pages/admin.js` (timeline wording only), `README.md`, `tests/test_yolo_mode.py`, this file.

## What was built

`config.json` → `vision.mode`: `"tags"`, `"yolo"` or `"fusion"` (default `"fusion"`, which is exactly what ran
before). The repo config keeps `features.yolo: false`, so nothing changes until the model is installed.

| mode | worker | backend counting (`shelf_state`) | misplaced |
|---|---|---|---|
| `tags` | ArUco only, YOLO never loaded (even with `features.yolo` true) | tag counts; `yolo_counts` ignored | by tag |
| `fusion` | tags + YOLO (needs `features.yolo` and the model, else tags) | `max(tag, yolo)` per bay per SKU (9.4) | by tag |
| `yolo` | **no ArUco detector at all**: every bay's `units` is `[]`, YOLO counts feed stability, motion freeze, the snapshot, the evidence crops and the clips | YOLO counts only | a SKU counted in a bay that is not its home bay (`tag_id: null` in the warning) |

`"yolo"` with `features.yolo` false, or with the model missing, falls back to tags with a loud warning
(`vision.fusion` / `backend.settings.effective_vision_mode`), so the core loop never breaks and the shelf is never
silently blind. A bad `vision.mode` value aborts startup (`SettingsError`) and the worker (`ValueError`).

Tag free evidence (item 2 of the brief):

- `vision/evidence.py`: a bay's content for "did it change" is now (stable units, stable YOLO counts), so a count
  move produces a `change` crop. Each crop carries `yolo_counts` and `boxes` (`sku -> [[x, y, w, h, conf], ...]`
  in crop pixels) from this frame's YOLO boxes whose center is in the crop.
- `POST /internal/evidence` accepts optional `yolo_counts` and `boxes` (stored in the new `evidence.yolo_json`
  column). `disputes.build_evidence` outlines the missing unit from the tag as before; **when no tag is known it
  outlines the YOLO box that disappeared** (`yolo_outline`: the box in the newest crop that had more boxes of the
  SKU than the latest crop, least overlapping the boxes still there). `evidence_json` also records
  `outline_from` (`tag` | `yolo`), `count_before` and `count_now`.
- `POST /internal/clips` accepts optional `counts_before` / `counts_after` (new columns); the worker fills them
  from the stable counts around the change; `clip_view` returns them.
- `review.gather`: the timeline gets `detection` (`tags` | `yolo`), `baseline_counts`, `latest_counts`,
  `missing_count`, `yolo_counts` on every photo and change, `counts_before/after` on every clip; the most
  relevant clip is chosen by the SKU's count dropping when there is no tag id. The system prompt explains the
  tag free case. The admin timeline and clip cards print counts instead of tag ids when `detection` is `yolo`.

Overlay: top line is `MODE yolo (no tags) | FPS ...` (or `MODE fusion (tags + YOLO)` / `MODE tags`); YOLO boxes
with class and confidence are drawn in every mode where YOLO runs; in `yolo` mode the bay line shows the counts
(`2 units elx:2`) and the pending line shows pending counts.

Capture (`training/capture.py`): with `vision.mode` `"yolo"` the preview shows `No tags: remove all tags before
capturing` (red while a tag is in view), wants 100 % tag free frames, and warns at the end how many frames show
a tag. New `e` key toggles "empty shelf" marking; the counter `empty shelf frames: N/M (want ~10%)` turns orange
below 10 % once 10 frames are saved. `TRAINING.md` is rewritten for tag free capture with the five class names
taken from `catalog.json`.

Dev scripts: `fake_shelf.py --yolo-only` and `e2e_sim.py --yolo-only` post `yolo_counts` with empty `units`
(the sim refuses to run against a backend whose config would count an empty shelf).

## Decisions (please confirm in the morning)

1. **Section 8.2 additions, not edited in the spec.** CLAUDE.md says never change Section 8 without asking, so
   the spec text is untouched. The code adds *optional, backwards compatible* fields the brief needs:
   `/internal/evidence` body → `yolo_counts`, `boxes`; `/internal/clips` body → `counts_before`,
   `counts_after`; Dispute `clips[]` → `counts_before`, `counts_after`; the admin `timeline` → `detection`,
   `baseline_counts`, `latest_counts`, `missing_count`, per photo / clip counts. CartSnapshot `warnings[]` is
   unchanged in shape; `tag_id` is `null` in yolo mode. If you agree, paste the four bullet points above into
   8.2 / 8.13 / 8.14; if not, say which to drop.
2. **Misplaced in fusion mode stays tag based** (as today). YOLO based misplaced detection runs only in `yolo`
   mode, so a fusion shelf behaves exactly as before the change.
3. **`tags` mode ignores `yolo_counts` entirely**, even with `features.yolo` true, and never loads the model.
   Before this change the flag alone decided; `fusion` is the default so nothing moves for existing configs.
4. **DB columns** added through the existing `ADDED_COLUMNS` migration (`evidence.yolo_json`,
   `clips.counts_before_json`, `clips.counts_after_json`). Existing databases upgrade on start.
5. **Empty shelf frames are marked by hand** (`e` key) rather than guessed from the image: capture runs without
   the model, so there is nothing else to count them with.

## Morning checklist

- [ ] Sign off decision 1 (spec 8.2 / 8.13 / 8.14 wording) and decisions 2 and 3.
- [ ] Peel every tag off, set `"mode": "yolo"` under `vision`, run `python -m training.capture`: reminder line
      visible, `no tags visible` wants 100 %, press `e` while clearing the bays, ~10 % empty frames.
- [ ] Label, train and install the model per `training/TRAINING.md` sections 2 to 6 (five classes).
- [ ] Set `"yolo": true` under `features`, keep `"mode": "yolo"`, `.\scripts\run_all.ps1`. Worker log shows
      `vision mode: yolo`, the overlay top line reads `MODE yolo (no tags)`, magenta boxes on every product,
      bay lines show `elx:2` style counts, no cyan tag outlines anywhere.
- [ ] Ten picks and put backs per bay with no tags: 9/10 or better. Put a protein bar in the electrolyte bay:
      the phone says "Protein bar is in the wrong bay", the cart stays empty.
- [ ] Hand over a bay while nothing moves: bay yellow (MOTION), counts unchanged after the hand leaves.
- [ ] Open a dispute ("Not mine?") on an item still off the shelf: the red outline sits on the product that
      disappeared in the baseline photo; the admin timeline lines read `counts elx:2 → elx:1`.
- [ ] Without the camera: `python scripts\fake_shelf.py --yolo-only loop` (type `4` + Enter: one protein bar in
      the cart), then `python scripts\e2e_sim.py --yolo-only` passes end to end.
- [ ] Set `"mode": "tags"` and restart: no YOLO line, tags only, exactly as before. Set `"fusion"`: max rule as
      before (S5.2 "cover the tags with tape" check).
- [ ] If the model file is missing with `"mode": "yolo"`, the worker prints the loud fallback warning and runs on
      tags (put the tags back for that case).
