"""Validate a YOLO dataset folder (Roboflow "YOLOv8" export) before training.

    python -m training.check_dataset <dataset folder>

The folder is the unzipped Roboflow export: data.yaml plus train/ valid/ test/, each with images/ and
labels/. Checks:
  - data.yaml exists and its class names match catalog.json yolo_class values exactly (order free;
    fusion maps boxes to SKUs by name), nc agrees with the names
  - every image has a label file (a missing one is a warning: YOLO treats it as background)
  - every label line is "class x_center y_center width height" with a valid class index and
    coordinates in 0..1 (polygon lines mean the export was a segmentation format: error)
  - label files without an image (warning)
  - boxes and images per class, per split and in total; warns when a class has fewer than 100 boxes

Exit code 0 when there are no errors (warnings allowed), 1 otherwise.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = ROOT / "catalog.json"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
MIN_BOXES_PER_CLASS = 100


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    # split -> class name -> count
    boxes: dict[str, Counter] = field(default_factory=dict)
    images_with_class: dict[str, Counter] = field(default_factory=dict)
    images: Counter = field(default_factory=Counter)  # split -> image count
    background: Counter = field(default_factory=Counter)  # split -> images with no boxes

    @property
    def ok(self) -> bool:
        return not self.errors

    def total_boxes(self) -> Counter:
        total: Counter = Counter()
        for c in self.boxes.values():
            total.update(c)
        return total


def catalog_classes(path: Path = CATALOG_PATH) -> list[str]:
    catalog = json.loads(path.read_text(encoding="utf-8"))
    return [s["yolo_class"] for s in catalog["skus"] if s.get("yolo_class")]


# --- data.yaml --------------------------------------------------------------------------------


def _parse_names_fallback(text: str) -> tuple[list[str] | None, int | None]:
    """Minimal reader for the names / nc keys when PyYAML is not installed. Handles
    names: ['a', 'b'], a block list (- a) and a block mapping (0: a)."""
    names: list[str] | None = None
    nc: int | None = None
    lines = text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.split("#", 1)[0].rstrip()
        if stripped.startswith("nc:"):
            try:
                nc = int(stripped[3:].strip())
            except ValueError:
                pass
        if not stripped.startswith("names:"):
            continue
        rest = stripped[len("names:"):].strip()
        if rest:
            value = ast.literal_eval(rest)
            names = [str(v) for v in (value.values() if isinstance(value, dict) else value)]
            continue
        items: list[tuple[int, str]] = []
        for sub in lines[i + 1:]:
            s = sub.split("#", 1)[0].strip()
            if not s:
                continue
            if not sub[:1].isspace() and not s.startswith("-"):
                break
            if s.startswith("-"):
                items.append((len(items), s[1:].strip().strip("'\"")))
            elif ":" in s:
                k, v = s.split(":", 1)
                items.append((int(k.strip()), v.strip().strip("'\"")))
        names = [v for _, v in sorted(items)]
    return names, nc


def read_data_yaml(path: Path) -> tuple[list[str] | None, int | None]:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # PyYAML ships with ultralytics
    except ImportError:
        return _parse_names_fallback(text)
    data = yaml.safe_load(text) or {}
    names = data.get("names")
    if isinstance(names, dict):
        names = [names[k] for k in sorted(names)]
    nc = data.get("nc")
    return (None if names is None else [str(n) for n in names]), (None if nc is None else int(nc))


# --- labels ------------------------------------------------------------------------------------


def check_label_file(path: Path, n_classes: int) -> tuple[list[int], list[str]]:
    """(class ids of the boxes, problems). An empty file is a valid background image."""
    classes: list[int] = []
    problems: list[str] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        parts = line.split()
        if not parts:
            continue
        where = f"{path.name}:{lineno}"
        if len(parts) != 5:
            kind = "a polygon (segmentation export; re-export as YOLOv8 object detection)" if len(parts) > 5 else "too short"
            problems.append(f"{where}: {len(parts)} values, expected 5 ({kind})")
            continue
        try:
            cls = int(parts[0])
            x, y, w, h = (float(v) for v in parts[1:])
        except ValueError:
            problems.append(f"{where}: not numbers: {line.strip()!r}")
            continue
        if not 0 <= cls < n_classes:
            problems.append(f"{where}: class {cls} out of range 0..{n_classes - 1}")
            continue
        if not all(0.0 <= v <= 1.0 for v in (x, y, w, h)) or w <= 0 or h <= 0:
            problems.append(f"{where}: box {x:g} {y:g} {w:g} {h:g} not normalized to 0..1")
            continue
        classes.append(cls)
    return classes, problems


def find_splits(dataset: Path) -> dict[str, tuple[Path, Path]]:
    """split name -> (images dir, labels dir). Looks for <split>/images and images/<split> layouts."""
    splits: dict[str, tuple[Path, Path]] = {}
    for images in sorted(p for p in dataset.rglob("images") if p.is_dir()):
        labels = images.parent / "labels"
        name = images.parent.name if images.parent != dataset else "all"
        splits[name] = (images, labels)
    # ultralytics style: images/train, labels/train
    top_images = dataset / "images"
    if top_images.is_dir():
        subdirs = [d for d in top_images.iterdir() if d.is_dir()]
        if subdirs:
            splits.pop("all", None)
            for d in sorted(subdirs):
                splits[d.name] = (d, dataset / "labels" / d.name)
    return splits


def check_dataset(dataset: Path, expected: list[str] | None = None, min_boxes: int = MIN_BOXES_PER_CLASS) -> Report:
    rep = Report()
    expected = catalog_classes() if expected is None else expected
    data_yaml = dataset / "data.yaml"
    if not data_yaml.is_file():
        rep.errors.append(f"{data_yaml} not found (unzip the Roboflow export so data.yaml is at the top)")
        return rep
    try:
        names, nc = read_data_yaml(data_yaml)
    except Exception as exc:  # malformed YAML of any kind
        rep.errors.append(f"cannot read {data_yaml}: {exc}")
        return rep
    if not names:
        rep.errors.append("data.yaml has no names list")
        return rep
    rep.names = names
    if nc is not None and nc != len(names):
        rep.errors.append(f"data.yaml nc is {nc} but it lists {len(names)} names")
    missing = sorted(set(expected) - set(names))
    extra = sorted(set(names) - set(expected))
    if missing:
        rep.errors.append(f"classes in catalog.json yolo_class but not in the dataset: {missing}")
    if extra:
        rep.errors.append(f"classes in the dataset that no catalog.json SKU uses: {extra} "
                          "(rename them in Roboflow to match yolo_class exactly)")
    if len(set(names)) != len(names):
        rep.errors.append(f"duplicate class names in data.yaml: {names}")

    splits = find_splits(dataset)
    if not splits:
        rep.errors.append(f"no images/ folder found under {dataset}")
        return rep
    for split, (img_dir, lbl_dir) in splits.items():
        boxes, with_class = Counter(), Counter()
        images = sorted(p for p in img_dir.iterdir() if p.suffix.lower() in IMAGE_EXTS)
        rep.images[split] = len(images)
        if not images:
            rep.warnings.append(f"{split}: no images in {img_dir}")
        if not lbl_dir.is_dir():
            rep.errors.append(f"{split}: labels folder {lbl_dir} is missing")
            continue
        missing_labels = []
        for img in images:
            label = lbl_dir / (img.stem + ".txt")
            if not label.is_file():
                missing_labels.append(img.name)
                rep.background[split] += 1
                continue
            classes, problems = check_label_file(label, len(names))
            rep.errors.extend(f"{split}/labels/{p}" for p in problems)
            if not classes:
                rep.background[split] += 1
            for cls in classes:
                boxes[names[cls]] += 1
            for cls in set(classes):
                with_class[names[cls]] += 1
        if missing_labels:
            rep.warnings.append(f"{split}: {len(missing_labels)} image(s) without a label file (treated as "
                                f"background), e.g. {missing_labels[:3]}")
        stems = {p.stem for p in images}
        orphans = sorted(p.name for p in lbl_dir.glob("*.txt") if p.stem not in stems)
        if orphans:
            rep.warnings.append(f"{split}: {len(orphans)} label file(s) without an image, e.g. {orphans[:3]}")
        rep.boxes[split] = boxes
        rep.images_with_class[split] = with_class

    total = rep.total_boxes()
    for name in names:
        if total[name] < min_boxes:
            rep.warnings.append(f"class {name!r} has only {total[name]} boxes (want at least {min_boxes})")
    if not any(k in splits for k in ("valid", "val")):
        rep.warnings.append("no valid/ split: training will have nothing to report mAP50 on")
    return rep


def format_report(rep: Report) -> str:
    lines = []
    if rep.names:
        lines.append(f"classes: {rep.names}")
        splits = list(rep.boxes)
        header = f"{'class':<18}" + "".join(f"{s:>12}" for s in splits) + f"{'total':>12}"
        lines.append("boxes per class (images containing it):")
        lines.append(header)
        total = rep.total_boxes()
        for name in rep.names:
            row = f"{name:<18}"
            for s in splits:
                row += f"{f'{rep.boxes[s][name]} ({rep.images_with_class[s][name]})':>12}"
            lines.append(row + f"{total[name]:>12}")
        lines.append(f"{'images':<18}" + "".join(f"{rep.images[s]:>12}" for s in splits)
                     + f"{sum(rep.images.values()):>12}")
        lines.append(f"{'background':<18}" + "".join(f"{rep.background[s]:>12}" for s in splits)
                     + f"{sum(rep.background.values()):>12}")
    for w in rep.warnings:
        lines.append(f"WARNING: {w}")
    shown = rep.errors[:30]
    for e in shown:
        lines.append(f"ERROR: {e}")
    if len(rep.errors) > len(shown):
        lines.append(f"ERROR: ... and {len(rep.errors) - len(shown)} more")
    lines.append("RESULT: OK" if rep.ok else f"RESULT: {len(rep.errors)} error(s)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m training.check_dataset", description=__doc__.split("\n\n")[0])
    parser.add_argument("dataset", type=Path, help="unzipped Roboflow YOLOv8 export (folder with data.yaml)")
    parser.add_argument("--min-boxes", type=int, default=MIN_BOXES_PER_CLASS)
    args = parser.parse_args(argv)
    rep = check_dataset(args.dataset, min_boxes=args.min_boxes)
    print(format_report(rep))
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
