# YOLO training guide (F13)

Goal: a YOLO11n model that finds the three products on the shelf even when their ArUco tags are
covered, saved as `models/speedmart_yolo.pt`. Acceptance (S5.1): validation **mAP50 >= 0.90 for every
class**.

Time budget: capture 30 min, label 60 to 90 min, train 15 to 25 min (Colab GPU, mostly waiting).

All commands are for Windows PowerShell, run from the repo root with the venv active:

```powershell
cd D:\SpeedMart
.venv\Scripts\Activate.ps1
```

Class names used everywhere (they come from `catalog.json` `yolo_class`, spelled exactly like this):

| SKU | Product | YOLO class name |
|---|---|---|
| `elx` | Electrolyte tabs | `electrolytes` |
| `rec` | Recovery drink | `recovery_drink` |
| `bar` | Protein bar | `protein_bar` |

---

## 0. Before you start

1. Camera mounted in its final position over the shelf, bays calibrated (`python -m vision.calibrate`).
2. Exposure locked and saved: `python -m vision.camera --preview`, adjust with `[` `]` if needed, press
   `s`, then `q`. The capture tool uses the same settings, so training images look like live frames.
3. Close the vision worker (only one program can hold the camera).
4. Accounts: Roboflow (free) and Google (for Colab).

## 1. Capture 250 to 400 frames

```powershell
python -m training.capture
```

- `space` turns auto save on or off (one frame every 0.5 s). `s` saves a single frame. `q` quits.
- Frames go to `training\raw\<date-time>\`. The green bay boxes and counters are drawn on the preview
  only, never on the saved images.
- Target: **250 to 400 frames** in total (several runs are fine; each run gets its own folder).

> **IMPORTANT: at least half of the frames must have the ArUco tags covered or removed.**
> Otherwise YOLO learns to find the black and white tags instead of the products, and it will fail
> exactly when we need it (tag covered by a hand or glare). Peel the tags off, or cover them with a
> sticky note or painter's tape, for at least half the capture. The capture window shows
> `no tags visible: N/M (want >= 50%)` and turns orange while you are below half.

Vary the scene while auto save runs (move something every second or two):

- each product **alone** in its bay, and in other bays
- **groups**: two of the same, all three products, products touching each other
- **different positions**: centre, edges and corners of each bay, rotated, lying down and standing up
- **partly covered by a hand** (fingers over half the product), hand reaching in and out
- **held above the shelf** at different heights, in a hand, as if being picked
- **bright and dim light**: room lights on and off, a phone flashlight from the side, a shadow cast over
  one bay
- a few frames of the **empty shelf** and of hands with nothing in them (teaches "no product here")
- tags visible for the other half, so the model does not treat the tag as a warning sign either

At the venue, capture about **50 more frames** under the expo lighting and add them to the dataset.

Count what you have:

```powershell
(Get-ChildItem training\raw -Recurse -Filter *.jpg).Count
```

## 2. Label in Roboflow

1. roboflow.com > **Create New Project** > type **Object Detection**, any name (e.g. `speedmart`).
2. **Upload**: drag every folder from `training\raw\` into the upload page. Save and continue.
3. **Classes**: create exactly `electrolytes`, `recovery_drink`, `protein_bar` (lower case, underscore,
   no spaces, no plural changes). A different spelling means the product is never mapped to its SKU.
4. **Annotate**: draw a tight box around **every visible product** in every image, including partly
   covered ones, ones held in a hand, and ones outside the bays. Box the product, not the tag. Leave empty
   shelf frames with no boxes (mark them as null / background when Roboflow asks).
   Tip: after ~30 images, Roboflow's **Label Assist** / auto-label can pre-draw boxes; check each one.
5. **Generate** a version:
   - Split: **80% train / 10% valid / 10% test**.
   - Preprocessing: Auto-Orient on; Resize off (or "Fit within 640x640"; never stretch).
   - Augmentation (outputs x3): **Brightness +/-25%**, **Exposure +/-15%**, **Blur up to 1 px**,
     **Rotation +/-10 degrees**.
6. **Export**: format **YOLOv8** (works for YOLO11), **Download zip to computer**.

## 3. Check the dataset (1 minute, catches most mistakes)

```powershell
Expand-Archive -Path $HOME\Downloads\<your-export>.zip -DestinationPath training\dataset -Force
python -m training.check_dataset training\dataset
```

It checks that the class names match `catalog.json`, that label files exist and are well formed
(5 numbers per line, class index valid, coordinates between 0 and 1), prints boxes per class per split,
and warns when a class has fewer than 100 boxes. `RESULT: OK` means good to train. Fix any `ERROR` in
Roboflow and export again. A `WARNING: ... fewer than 100 boxes` means capture and label more frames of
that product (or raise the augmentation outputs).

`training\dataset\` and `training\raw\` are gitignored; do not commit images.

## 4. Train in Colab

1. Open colab.research.google.com > **File > Upload notebook** > pick `training\train_colab.ipynb`.
2. **Runtime > Change runtime type > T4 GPU** > Save.
3. **Runtime > Run all**. Cell 3 opens a file picker: choose the same Roboflow zip.
4. The notebook installs Ultralytics, fixes the paths in `data.yaml`, checks the class names, then trains:

   ```
   model = YOLO("yolo11n.pt")
   model.train(data=data.yaml, epochs=80, imgsz=640, batch=16, patience=20)
   ```

   Command line equivalent (Colab or any GPU machine):

   ```
   yolo detect train model=yolo11n.pt data=/content/dataset/data.yaml epochs=80 imgsz=640 batch=16 patience=20
   ```

   `patience=20` stops early once validation has not improved for 20 epochs.

No GPU and no Colab? It trains on a laptop CPU too, just slowly (roughly 1 to 2 h for 300 images):

```powershell
pip install -r requirements-yolo.txt
yolo detect train model=yolo11n.pt data=training\dataset\data.yaml epochs=80 imgsz=640 batch=8 patience=20 device=cpu
```

(Roboflow's `data.yaml` uses `../train/images` style paths; if Ultralytics cannot find the images, edit
`training\dataset\data.yaml` to `train: train/images`, `val: valid/images`, `test: test/images` and add
`path: D:/SpeedMart/training/dataset`.)

## 5. Check mAP50

Cell 7 prints a table like:

```
class                    P       R   mAP50  mAP50-95
electrolytes         0.962   0.941   0.975     0.801  OK
protein_bar          0.951   0.930   0.968     0.774  OK
recovery_drink       0.940   0.922   0.955     0.760  OK
all                  0.951   0.931   0.966     0.778
PASS: every class mAP50 >= 0.90
```

- **mAP50** per class is the number that matters: at least **0.90 for every class**.
- Cell 6 shows `results.png`: losses should fall and flatten, `metrics/mAP50(B)` should rise and flatten.
  Without the notebook, the same numbers are in `runs/detect/train/results.csv` (column
  `metrics/mAP50(B)`) and the final `yolo detect val model=runs/detect/train/weights/best.pt data=...`
  prints the per class table.

If mAP50 is low for a class:
- more frames of that product in the hard cases (partly covered, in hand, dim light), then re-label and
  retrain;
- look at `confusion_matrix.png`: two products confused with each other need frames with both side by side;
- check boxes in Roboflow for missed or sloppy labels; one missed product per image hurts a lot.

## 6. Install the model

Cell 9 downloads `speedmart_yolo.pt`. Put it where `config.json` `vision.yolo_model` points:

```powershell
Move-Item $HOME\Downloads\speedmart_yolo.pt models\speedmart_yolo.pt -Force
pip install -r requirements-yolo.txt
```

(If you trained locally the file is `runs\detect\train\weights\best.pt`; copy it to
`models\speedmart_yolo.pt`.) `models\*.pt` is gitignored; share the file by USB or drive, not git.

Quick check on the live camera (vision worker closed), or on any saved image:

```powershell
python -m vision.yolo_detect
python -m vision.yolo_detect training\raw\<run>\<file>.jpg
```

It prints the device used (cuda / mps / cpu), every box with class and confidence, and per bay SKU counts.

## 7. Turn YOLO on

1. In `config.json` set `"yolo": true` under `features`. Optional tuning in `vision`:
   `yolo_conf` (0.55: raise if it sees products that are not there, lower if it misses covered ones) and
   `yolo_every_n_frames` (3: raise to 5 or 6 if the overlay FPS drops below 15 on a CPU laptop).
2. Restart everything (the backend reads the flag for fusion, the worker for inference):

   ```powershell
   .\scripts\run_all.ps1
   ```

   (or start `uvicorn backend.main:app --host 0.0.0.0 --port 8000` and `python -m vision.worker` in two
   terminals).
3. The overlay now draws YOLO boxes with class and confidence, and each bay line shows `yolo sku:n`.
   Per bay per SKU, the shelf count is `max(tag count, YOLO count)` (Section 9.4).
4. S5.2 acceptance: cover the tags with tape; 10 picks and put backs per bay should register 9/10 or
   better. With `"yolo": false` everything behaves exactly as before.

To turn it off again: `"yolo": false` and restart. If `ultralytics` is not installed or the model file is
missing, the worker logs one warning and runs tags only; nothing crashes.
