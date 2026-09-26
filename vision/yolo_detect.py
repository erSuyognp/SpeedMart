"""YOLO inference -> per-bay SKU counts (Section 14 step 5, 9.4, F13).

    python -m vision.yolo_detect              live camera, boxes + per bay counts (vision worker closed)
    python -m vision.yolo_detect <image>      one image: print every box and the per bay counts

The model is loaded once. update(frame) runs inference every vision.yolo_every_n_frames calls and returns
the cached result in between. Class names map to SKUs through catalog.json yolo_class; a box counts
for the bay whose ROI contains its center, only with conf >= vision.yolo_conf.

Device: "mps" on Apple Silicon, 0 (first CUDA GPU) when CUDA is available, else "cpu".

Never crashes the worker: if ultralytics is not installed or the model file is missing, load_yolo()
logs one clear warning and returns None, and the worker behaves as if features.yolo were false.
"""

from __future__ import annotations

import argparse
import json
import logging
import platform
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # allow  python vision/yolo_detect.py  too
    sys.path.insert(0, str(ROOT))

from vision.aruco_detect import in_roi  # noqa: E402

CONFIG_PATH = ROOT / "config.json"
CATALOG_PATH = ROOT / "catalog.json"
IMGSZ = 640
ERROR_LOG_EVERY_S = 10.0

log = logging.getLogger("vision.yolo")

# (class name, confidence, (x1, y1, x2, y2)) in frame pixels
RawBox = tuple[str, float, tuple[float, float, float, float]]
Predictor = Callable[[np.ndarray], Sequence[RawBox]]


@dataclass(frozen=True)
class YoloBox:
    cls_name: str
    sku: str | None  # None when the class is not in the catalog
    conf: float
    xyxy: tuple[float, float, float, float]
    bay: int | None  # bay whose ROI holds the center, None = outside every bay

    @property
    def center(self) -> tuple[float, float]:
        x1, y1, x2, y2 = self.xyxy
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)


@dataclass(frozen=True)
class YoloResult:
    boxes: tuple[YoloBox, ...] = ()
    per_bay: Mapping[int, Mapping[str, int]] = field(default_factory=dict)  # bay -> sku -> count
    frame_index: int = -1  # detector call on which this result was computed
    ok: bool = True  # False when inference raised


def class_to_sku(catalog: dict) -> dict[str, str]:
    """yolo_class -> sku from catalog.json skus."""
    return {s["yolo_class"]: s["sku"] for s in catalog["skus"] if s.get("yolo_class")}


def assign_boxes(raw: Iterable[RawBox], bays: list[dict], class_sku: Mapping[str, str],
                 conf_min: float) -> YoloResult:
    """Keep boxes with conf >= conf_min, map class -> SKU, place each by its center (first matching ROI,
    same half open rule as tags). Counts only boxes with a known SKU inside a bay. Every bay appears in
    per_bay, possibly empty."""
    per_bay: dict[int, dict[str, int]] = {int(b["id"]): {} for b in bays}
    boxes = []
    for cls_name, conf, xyxy in raw:
        conf = float(conf)
        if conf < conf_min:
            continue
        xyxy = tuple(float(v) for v in xyxy)
        center = ((xyxy[0] + xyxy[2]) / 2.0, (xyxy[1] + xyxy[3]) / 2.0)
        bay_id = next((int(b["id"]) for b in bays if in_roi(center, b["roi"])), None)
        sku = class_sku.get(str(cls_name))
        boxes.append(YoloBox(str(cls_name), sku, conf, xyxy, bay_id))
        if sku is not None and bay_id is not None:
            per_bay[bay_id][sku] = per_bay[bay_id].get(sku, 0) + 1
    return YoloResult(tuple(boxes), per_bay)


class YoloDetector:
    """Runs `predict` every `every_n` calls to update() and caches the last result. `predict` is the
    ultralytics model wrapped by ultralytics_predictor(), or a fake in tests."""

    def __init__(self, predict: Predictor, bays: list[dict], class_sku: Mapping[str, str], conf: float,
                 every_n: int = 3, device: str | int = "cpu", clock=time.monotonic):
        self.predict = predict
        self.bays = bays
        self.class_sku = dict(class_sku)
        self.conf = float(conf)
        self.every_n = max(1, int(every_n))
        self.device = device
        self.clock = clock
        self._calls = 0
        self._last = YoloResult(per_bay={int(b["id"]): {} for b in bays})
        self._unknown_logged: set[str] = set()
        self._last_error_log = -ERROR_LOG_EVERY_S
        self.last_inference_ms = 0.0

    @property
    def last(self) -> YoloResult:
        return self._last

    def update(self, frame: np.ndarray) -> YoloResult:
        index = self._calls
        self._calls += 1
        if index % self.every_n:
            return self._last
        started = self.clock()
        try:
            raw = list(self.predict(frame))
        except Exception as exc:  # a failing model must never take the camera loop down
            now = self.clock()
            if now - self._last_error_log >= ERROR_LOG_EVERY_S:
                self._last_error_log = now
                log.warning("YOLO inference failed (%s: %s); using tags only for this frame", type(exc).__name__, exc)
            # an empty result, not the stale one: stale YOLO counts could keep a picked item on the shelf
            self._last = YoloResult(per_bay={int(b["id"]): {} for b in self.bays}, frame_index=index, ok=False)
            return self._last
        self.last_inference_ms = (self.clock() - started) * 1000.0
        result = assign_boxes(raw, self.bays, self.class_sku, self.conf)
        for box in result.boxes:
            if box.sku is None and box.cls_name not in self._unknown_logged:
                self._unknown_logged.add(box.cls_name)
                log.warning("YOLO class %r is not a yolo_class in catalog.json; its boxes are ignored",
                            box.cls_name)
        self._last = YoloResult(result.boxes, result.per_bay, index, True)
        return self._last


# --- ultralytics glue (only imported when YOLO is on) --------------------------------------------


def select_device() -> str | int:
    """"mps" on Apple Silicon, 0 when CUDA is available, else "cpu". Never raises."""
    try:
        import torch
    except ImportError:
        return "cpu"
    try:
        if sys.platform == "darwin" and platform.machine() == "arm64" and torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return 0
    except Exception:  # odd torch builds
        pass
    return "cpu"


def ultralytics_predictor(model, conf: float, device: str | int, imgsz: int = IMGSZ) -> Predictor:
    """Wrap an ultralytics YOLO model as frame -> [(class name, conf, xyxy)]."""

    def predict(frame: np.ndarray) -> list[RawBox]:
        results = model.predict(frame, imgsz=imgsz, conf=conf, verbose=False, device=device)
        out: list[RawBox] = []
        for r in results:
            if r.boxes is None or len(r.boxes) == 0:
                continue
            xyxy = r.boxes.xyxy.cpu().numpy()
            confs = r.boxes.conf.cpu().numpy()
            classes = r.boxes.cls.cpu().numpy().astype(int)
            for (x1, y1, x2, y2), c, k in zip(xyxy, confs, classes):
                out.append((str(r.names[int(k)]), float(c), (float(x1), float(y1), float(x2), float(y2))))
        return out

    return predict


def load_yolo(config: dict, catalog: dict, root: Path = ROOT) -> YoloDetector | None:
    """The detector for this config, or None (after one warning) when ultralytics or the model file is
    missing or the model does not load. Call only when features.yolo is true."""
    vision = config["vision"]
    model_path = Path(vision.get("yolo_model", "models/speedmart_yolo.pt"))
    if not model_path.is_absolute():
        model_path = root / model_path
    problems = []
    if not model_path.is_file():
        problems.append(f"the model file {model_path} is missing (train one: training/TRAINING.md)")
    try:
        from ultralytics import YOLO
    except Exception as exc:  # ImportError, or a broken torch install
        YOLO = None
        problems.append(f"ultralytics cannot be imported ({exc}; install: pip install -r requirements-yolo.txt)")
    if problems:
        log.warning("WARNING: features.yolo is true but %s. Running with tags only (as if YOLO were off).",
                    " and ".join(problems))
        return None
    try:
        model = YOLO(str(model_path))
    except Exception as exc:
        log.warning("YOLO model %s failed to load (%s: %s). Running with tags only.", model_path,
                    type(exc).__name__, exc)
        return None
    device = select_device()
    conf = float(vision.get("yolo_conf", 0.55))
    class_sku = class_to_sku(catalog)
    names = set(getattr(model, "names", {}).values()) if isinstance(getattr(model, "names", None), dict) else set()
    if names and not set(class_sku) <= names:
        log.warning("YOLO model classes %s do not cover catalog yolo_class values %s; missing SKUs are "
                    "counted by tags only", sorted(names), sorted(class_sku))
    every_n = int(vision.get("yolo_every_n_frames", 3))
    log.info("YOLO loaded: %s on device %s, conf >= %g, every %d frames", model_path.name, device, conf, every_n)
    return YoloDetector(ultralytics_predictor(model, conf, device), config["bays"], class_sku, conf, every_n, device)


# --- CLI: check a trained model -------------------------------------------------------------------


def _print_result(result: YoloResult) -> None:
    for box in result.boxes:
        where = "outside bays" if box.bay is None else f"bay {box.bay}"
        sku = box.sku or "not in catalog"
        print(f"  {box.cls_name:<16} conf {box.conf:.2f}  {where:<13} sku {sku}")
    print(f"  per bay: {dict(result.per_bay)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m vision.yolo_detect", description="Try the trained YOLO model.")
    parser.add_argument("image", nargs="?", type=Path, help="image file; omit for the live camera")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    import cv2

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    config["vision"]["yolo_every_n_frames"] = 1 if args.image else config["vision"].get("yolo_every_n_frames", 3)
    detector = load_yolo(config, catalog)
    if detector is None:
        return 1
    width, height = int(config["camera"]["width"]), int(config["camera"]["height"])

    if args.image:
        frame = cv2.imread(str(args.image))
        if frame is None:
            print(f"cannot read {args.image}", file=sys.stderr)
            return 1
        if frame.shape[1] != width or frame.shape[0] != height:
            frame = cv2.resize(frame, (width, height))
        result = detector.update(frame)
        print(f"{args.image.name}: {len(result.boxes)} boxes ({detector.last_inference_ms:.0f} ms, device {detector.device})")
        _print_result(result)
        return 0

    from vision.camera import CameraError, open_camera
    from vision.overlay import draw_yolo_boxes

    window = "SpeedMart YOLO check"
    try:
        with open_camera(config) as camera:
            cv2.namedWindow(window, cv2.WINDOW_AUTOSIZE)
            last_print = 0.0
            while True:
                frame, _, _ = camera.read()
                if frame.shape[1] != width or frame.shape[0] != height:
                    frame = cv2.resize(frame, (width, height))
                result = detector.update(frame)
                view = frame.copy()
                for bay in config["bays"]:
                    x1, y1, x2, y2 = (int(v) for v in bay["roi"])
                    cv2.rectangle(view, (x1, y1), (x2, y2), (0, 200, 0), 2)
                    counts = result.per_bay.get(int(bay["id"]), {})
                    cv2.putText(view, f"bay {bay['id']} {dict(counts)}", (x1 + 4, y1 + 22),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 200, 0), 2, cv2.LINE_AA)
                draw_yolo_boxes(view, result.boxes)
                cv2.putText(view, f"device {detector.device}  {detector.last_inference_ms:.0f} ms/inference  q quit",
                            (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
                cv2.imshow(window, view)
                now = time.monotonic()
                if now - last_print > 2.0:
                    last_print = now
                    print(f"per bay: {dict(result.per_bay)}")
                key = cv2.waitKey(1) & 0xFF
                if key in (ord("q"), 27) or cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                    break
    except CameraError as exc:
        print(f"Camera error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    finally:
        cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    sys.exit(main())
