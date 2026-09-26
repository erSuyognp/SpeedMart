# Overnight vision work (unattended session)

Branch: `claude/overnight-vision-setup-d5517a`. Scope: `vision/`, `training/`, `scripts/fake_shelf.py`,
the `vision` and `camera` keys of `config.json`, their tests, and this file.

## Task 1: S2.3 motion freeze and bay stability

### What was built

- `vision/motion.py`: `MotionDetector`. Per bay, blurred (5x5 Gaussian) grayscale crop, `absdiff` with the
  previous crop, `changed_fraction = count(diff > 25) / area`, `motion = changed_fraction > vision.motion_threshold`,
  motion free once `vision.motion_settle_ms` has passed since the last motion frame. The crop is the ROI
  grown by `vision.motion_margin_px` (new key, default 40), clamped to the frame. Tag assignment still uses
  the plain ROI.
- `vision/worker.py`: `StabilityTracker` (9.3) replaces `AlwaysStable` as the worker default.
  `stable = motion_free AND candidate held >= need`, where `need = vision.stable_remove_ms` (new key,
  default 700) if the candidate lost anything compared to the last stable content, else `vision.stable_ms`.
  Unstable bays report the last stable units. Clock is injectable (`clock=` argument, or pass `now`).
- Overlay: bay yellow while unstable, green when stable; per bay lines show reported units, `chg <fraction>`,
  `MOTION` / `settling N ms` / `hold N/M ms` / `stable`, and `pending [...]` for an unconfirmed candidate.
  The motion area (ROI + margin) is a thin gray rectangle. Top left shows the current threshold, settle
  time and margin.
- Live tuning keys in the worker window: `[` / `]` motion_threshold -/+ 0.005 (floor 0.005),
  `-` / `=` motion_settle_ms -/+ 50 (floor 0), `s` writes both into `config.json` without touching any
  other key or the file's formatting.
- `features.motion_freeze: false`: no motion detector is created at all; every bay is motion free and
  stability timing (stable_ms / stable_remove_ms) still applies (Section 17).

### Decisions

1. **Where stability lives.** Spec 9.3 names `vision/worker.py`, so `StabilityTracker` is there (next to
   the existing `BayTracker` protocol), not in a new module.
2. **What counts as a removal.** Any unit id in the last stable content missing from the candidate, or any
   YOLO SKU count lower than in the last stable content. A swap (one tag out, another in) is a removal,
   so it waits the longer 700 ms. Before a bay has ever been stable (startup), everything is an addition
   (400 ms).
3. **Unstable bays report the last stable units** in the snapshot `units` field (and last stable
   `yolo_counts` when YOLO is on), as the task asked. Section 8.2 says `units` are the tags in the ROI;
   the backend ignores unstable bays anyway (7.3), so the cart result is identical, and the admin view
   no longer shows flicker. Before any stable reading a bay reports `[]` with `stable: false`
   (ignored by the backend).
4. **Hold timer vs motion.** Implemented exactly as 9.3: the candidate timer starts when content changes,
   independently of motion; stability additionally needs motion free. So after a hand leaves, a bay is
   confirmed as soon as settle time has passed AND the candidate has held long enough.
5. **First frame / resized frame** has `changed_fraction = 0` (no previous crop to compare).
6. **Neighbouring margins overlap** with the current ROIs (bays are 40 px apart, margin 40 px). Motion in
   the gap freezes both neighbours. That is the safe direction (freeze, never a false pick). If it gets
   annoying, lower `vision.motion_margin_px` to 20.
7. **Float slack.** Hold and settle comparisons add 0.001 ms so a hold of exactly `stable_ms` counts;
   irrelevant in real time, it keeps the fake clock tests exact.
8. **Saving config.** `vision/camera.py` got a generic `save_section_settings(section, values)`; the old
   `save_camera_settings` now calls it. Same in-place literal rewrite and atomic replace as before.
9. **Snapshot `yolo_counts`** stays `{}` when YOLO is off (the existing S2.2 test expects the key present;
   the backend defaults it to `{}` anyway).

## Task 2: YOLO data tooling (Section 14)

### What was built

- `training/capture.py` (`python -m training.capture`): same `open_camera(config)` as the worker (locked
  exposure), frames resized to config size like the worker. `space` toggles auto save every 0.5 s
  (`--interval`), `s` saves one, `q` quits. Saves JPEG q95 into `training/raw/<YYYYmmdd-HHMMSS>/`,
  file names prefixed with the run name so several runs can be uploaded together. Preview overlay (bay
  boxes, saved count vs 250 to 400 target, AUTO indicator) is never written into the images.
- `training/check_dataset.py` (`python -m training.check_dataset <folder>`): validates a Roboflow YOLOv8
  export: data.yaml names vs catalog `yolo_class`, nc, label files present and well formed, orphans,
  boxes and images per class per split, warning under 100 boxes per class. Exit 1 on errors.
- `training/train_colab.ipynb`: GPU check, `pip install ultralytics>=8.3`, upload + unzip the Roboflow
  zip, rewrite data.yaml paths, class name check, `YOLO("yolo11n.pt").train(epochs=80, imgsz=640,
  batch=16, patience=20)`, results.png, per class P / R / mAP50 / mAP50-95 table with PASS at 0.90,
  sample predictions, download as `speedmart_yolo.pt`.
- `training/TRAINING.md`: the morning procedure, Windows commands.
- `training/data.yaml`: reference copy with the three class names (the real one comes in the export).

### Decisions

1. **No-tag counter in capture.** The capture tool runs the ArUco detector on each frame and counts saved
   frames with no tag visible, shown as `no tags visible: N/M (want >= 50%)`. It enforces the "at least
   half without tags" rule from the task without extra steps.
2. **Model file name** follows `config.json` `vision.yolo_model` = `models/speedmart_yolo.pt` (the spec
   text says `aisle_yolo.pt`; the project was renamed, config wins).
3. **Class order** does not matter anywhere: fusion maps by class *name*. data.yaml uses Roboflow's
   alphabetical order.
4. **Gitignore for images**: `training/raw/.gitignore` and `training/dataset/.gitignore` (both `*`),
   since the root `.gitignore` is outside this session's scope.
5. **PyYAML optional** in check_dataset: uses it if installed (it is, via uvicorn[standard] and
   ultralytics), else a small fallback parser for `names` / `nc`.
6. **Colab dataset input**: file upload of the zip (no API key in the notebook). The Roboflow download
   snippet is mentioned as an alternative with a warning not to share a notebook with the key in it.
7. **Missing label file** = warning (Ultralytics treats it as background), not an error.

## Task 3: S5.2 YOLO inference and fusion (flag stays off)

### What was built

- `vision/yolo_detect.py`: `load_yolo(config, catalog)` loads the model once; `YoloDetector.update(frame)`
  runs inference every `vision.yolo_every_n_frames` calls (imgsz 640, conf `vision.yolo_conf`) and
  returns the cached result in between. Class names map to SKUs via catalog `yolo_class`; boxes are
  assigned to the bay whose ROI holds their center (same half open rule as tags). Device: `"mps"` on
  Apple Silicon, `0` when CUDA is available, else `"cpu"`. CLI: `python -m vision.yolo_detect [image]`
  to try a trained model.
- Never crashes: missing ultralytics and/or missing model file -> one warning line naming both problems,
  `load_yolo` returns `None`, worker runs tags only. A model that fails to load -> same. An exception
  during inference -> rate limited warning, empty counts for that run.
- `vision/fusion.py`: 9.4 rule, `fused = max(tag_count, yolo_count)` per bay per SKU, plus totals.
- Worker: `start_yolo()` only when `features.yolo` is true; YOLO per bay counts go into the stability
  content (`StabilityTracker.update(..., yolo_counts=)`), and into the snapshot `yolo_counts` (last stable
  counts, like units). Overlay draws YOLO boxes (magenta, class + confidence; gray for classes not in
  the catalog), bay lines show `yolo sku:n`, the top line shows `YOLO on (device, every N)` or
  `YOLO unavailable (see log), tags only`.
- `requirements-yolo.txt` with `ultralytics>=8.3`; `requirements.txt` untouched.
- `scripts/fake_shelf.py`: new `--yolo` flag (sends yolo_counts as a perfect YOLO would) and a `cover <tag>`
  command / `c<tag>` in loop mode (tag hidden, product still there), to exercise backend fusion without a
  camera.
- Tests use a fake predictor and a fake `ultralytics` module; nothing needs the real package.

### Decisions

1. **backend/shelf_state.py was not changed.** It already applies 9.4 (per bay per SKU
   `max(tag, yolo)` when `features.yolo` is on, tag counts when off) and keeps last stable `yolo_counts`.
   A test (`test_fusion_matches_backend_counts`) checks that `vision/fusion.py` and the backend agree
   with the flag on and off.
2. **Inference failure returns empty counts, not the cached ones.** Stale YOLO counts could keep a picked
   item "on the shelf" (fused max). Empty counts fall back to tags, the safe direction.
3. **A YOLO count drop is a removal** for the asymmetric timing (needs `stable_remove_ms`), same as a tag
   disappearing. Zero counts are dropped from the content so `{}` and `{"elx": 0}` compare equal.
4. **Boxes outside every bay** (in a hand) are drawn but not counted, like `loose_units` for tags.
   Boxes of unknown classes are drawn gray, ignored, and logged once per class.
5. **CUDA device** is `0` (Ultralytics' first GPU), as in the S5.2 text.
6. **Missing-model check runs before the ultralytics import** but both are always checked, so the one
   warning names every missing piece.

## Verification run tonight (no camera)

- `pytest -q`: all tests pass (131 at the time of writing: 62 existing + 24 motion/stability +
  19 training tools + 26 YOLO/fusion).
- Worker loop smoke test with a fake camera (synthetic ArUco frames, a flickering "hand" over bay 0):
  bays confirm after 400 ms, bay 0 goes MOTION and keeps its units `[0, 1]` while the hand moves,
  returns to stable after it leaves; other bays stay stable. With `features.yolo: true` and no model or
  ultralytics: exactly one warning, loop runs on tags.
- Capture loop smoke test with a fake camera and scripted keys: auto save and `s` wrote clean 1280x720
  JPEGs into a timestamped folder.
- Overlay rendered to an image and inspected: yellow/green bays, chg / MOTION / hold / pending lines,
  gray motion areas, tuning line.

## Morning checklist

All commands: Windows PowerShell, repo root, venv active (`.venv\Scripts\Activate.ps1`).

### 0. Get the branch

1. `git fetch origin`
2. `git checkout claude/overnight-vision-setup-d5517a` (or merge it into your working branch).
3. `pip install -r requirements.txt -r requirements-dev.txt`
4. `pytest -q` -> expected: all pass, no failures.

### 1. S2.3 motion freeze and stability (camera + shelf + a hand)

Start both processes (two PowerShell windows):

```powershell
uvicorn backend.main:app --host 0.0.0.0 --port 8000
python -m vision.worker
```

(`scripts\run_all.ps1` does not exist on this branch yet; see section 5.) Open the admin page
(`http://localhost:8000/admin.html`), log in, demo-login, start a session so the cart is visible.

| # | Do | Expect |
|---|---|---|
| 1.1 | Nobody near the shelf for 5 s | All bays green, `stable`, `chg` below 0.01. Top line `motion thr 0.02  settle 300 ms  margin 40px`. |
| 1.2 | If a bay stays yellow / `MOTION` with nobody moving (light flicker, noise) | Press `]` until it stays green (each press +0.005), then `s`. Console prints `saved motion_threshold ... to config.json`. |
| 1.3 | Wave a hand above bay 0 without touching | Bay 0 yellow `MOTION`, then `settling N ms`, then green. Cart unchanged. Bay 1 may go yellow too when the hand is in the gap (margins overlap by design). |
| 1.4 | Hover a hand over bay 0 covering the tag for 3 s, do not lift the item | Cart does not change. Overlay: `pending [..]` under bay 0 while covered, the `N units [..]` line keeps the old units. |
| 1.5 | Lift the item and hold it above the shelf, then take the hand away | Item enters the cart once, about 1 s after the hand leaves (settle 300 ms + removal hold 700 ms). |
| 1.6 | Put it back | Leaves the cart about 0.7 s after the hand leaves (addition hold 400 ms). |
| 1.7 | 10 picks + 10 put backs on each bay | 10/10 correct per bay, no flicker in the cart. Write down any miss and what the overlay showed. |
| 1.8 | Set `"motion_freeze": false` in config.json, restart the worker | Top line `motion freeze OFF`, bays still confirm (400 ms / 700 ms), more flicker risk. Set it back to `true` and restart. |

Tuning if 1.4 fails (a still hand gets confirmed as a pick): raise `vision.stable_remove_ms` to 900 to
1200, or lower `motion_threshold` with `[`. If picks feel slow: lower `stable_remove_ms` to 500. If the
neighbour bay freezing is annoying: `vision.motion_margin_px` 20. `stable_ms`, `stable_remove_ms` and
`motion_margin_px` are edited in config.json and need a worker restart; threshold and settle are live
(`[ ] - =`, `s` saves).

### 2. YOLO data capture and training (camera + products + Roboflow + Colab)

Follow `training/TRAINING.md`. Checkpoints:

| # | Do | Expect |
|---|---|---|
| 2.1 | Close the worker. `python -m training.capture` | Window with green bay boxes, `saved 0 (target 250-400)`, `tags visible now: N`. |
| 2.2 | `space`, move products around, `space` again | Red AUTO dot while on; count rises about 2 per second; files appear in `training\raw\<date-time>\`. Saved images have no boxes or text on them. |
| 2.3 | Capture with tags covered/removed | `no tags visible` share reaches at least 50% (line turns green). |
| 2.4 | `(Get-ChildItem training\raw -Recurse -Filter *.jpg).Count` | 250 to 400. |
| 2.5 | Label + export in Roboflow (YOLOv8), unzip to `training\dataset`, `python -m training.check_dataset training\dataset` | `RESULT: OK`, no class under 100 boxes (else capture/label more). |
| 2.6 | Colab: upload `training\train_colab.ipynb`, GPU runtime, Run all, upload the zip | Cell 7 ends with `PASS: every class mAP50 >= 0.90`. Browser downloads `speedmart_yolo.pt`. |
| 2.7 | `Move-Item $HOME\Downloads\speedmart_yolo.pt models\speedmart_yolo.pt -Force` | File exists at `models\speedmart_yolo.pt`. |

### 3. S5.2 YOLO on (after 2.7)

| # | Do | Expect |
|---|---|---|
| 3.1 | `pip install -r requirements-yolo.txt` (large: pulls PyTorch. For an NVIDIA GPU install the CUDA build of torch from pytorch.org first) | Installs without errors. |
| 3.2 | `python -m vision.yolo_detect training\raw\<run>\<file>.jpg` | Prints `device cpu` (or `0` / `mps`), each box with class, conf and bay, and `per bay: {...}` matching the photo. |
| 3.3 | `python -m vision.yolo_detect` (live) | Magenta boxes on the products, per bay counts; `q` quits. |
| 3.4 | config.json `"yolo": true`, restart backend AND worker | Worker log: `YOLO loaded: speedmart_yolo.pt on device ...`. Overlay top line `YOLO on (cpu, every 3)`, magenta boxes with class + conf, bay lines show `yolo elx:2` etc. FPS still at least 15 (else set `yolo_every_n_frames` to 5 or 6). |
| 3.5 | Admin page shelf state | `yolo_counts` per bay filled. |
| 3.6 | Cover every tag with tape; 10 picks + put backs per bay | At least 9/10 register correctly (S5.2 acceptance). |
| 3.7 | Bays keep flipping yellow with `hold` restarting while nothing moves | YOLO count is flickering: raise `yolo_conf` (0.6 to 0.7) or retrain with more frames of that product. |
| 3.8 | `"yolo": false`, restart both | Behaves exactly as in section 1; no YOLO line or boxes. |
| 3.9 | Without the model: rename `models\speedmart_yolo.pt`, `"yolo": true`, restart the worker | One `WARNING: features.yolo is true but the model file ... is missing` line, top line `YOLO unavailable (see log), tags only`, everything else works. Rename it back. |

Backend fusion check without a camera (backend running, worker stopped):

```powershell
python scripts\fake_shelf.py --yolo loop
```

Type `c1` + Enter (cover tag 1): with `"yolo": true` the cart does not change; with `"yolo": false`
Electrolyte tabs enter the cart. Type `1` + Enter (really remove unit 1): it enters the cart either way.

### 4. Things to eyeball once

- The gray rectangles around each bay (motion area) should not reach into areas where people stand or
  where the laptop screen is visible; if they do, lower `motion_margin_px`.
- Printed threshold after `s`: `config.json` `vision.motion_threshold` / `motion_settle_ms` changed, every
  other line identical (`git diff config.json`).

### 5. Outside this session's scope (for whoever owns them)

- `scripts/run_all.ps1` is required by CLAUDE.md but does not exist on this branch (`run_all.sh` does).
  TRAINING.md and this checklist give the two-window fallback.
- The root `.gitignore` could list `training/raw/` and `training/dataset/`; for now each folder has its
  own `.gitignore`.
- Other overnight sessions may also edit `config.json`: this branch only adds `vision.stable_remove_ms`
  and `vision.motion_margin_px`; resolve merge conflicts by keeping both.
