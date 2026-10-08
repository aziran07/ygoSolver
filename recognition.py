"""Local Yu-Gi-Oh card recognition with the published DRAW2 ONNX models.

Pipeline: oriented YOLO card detector (upright pass plus 90/180/270-degree
passes for cards the upright pass misses) -> perspective-warped card crops ->
ViT classifier with 13,820 output classes (13,819 unique card IDs). Runs on
CPU via ONNX Runtime.

Models: https://huggingface.co/HichTala/draw2 (AGPL-3.0). The pre/post
processing constants (640 letterbox, RGB/255, ViT mean/std 0.5, YOLO output
layout, portrait box normalisation, square 224 crop) follow the upstream
browser pipeline docs/scripts/pipeline.js at commit
43b0a1c5fe5a987bb98d64ce7635c5581b72c9fb (AGPL-3.0). The code here is a
re-implementation; the upstream rotation heuristic and its "skip softmax when
logits look like probabilities" shortcut are deliberately not used.

`card_id` in results is the 8-digit card passcode from the DRAW2 label file,
not Konami's database cid.
"""

import hashlib
import json
import math
import os
import time
import urllib.request
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort
from PIL import Image

MODEL_DIR = Path("data/models")

HF_REVISION = "1030c147c4d0c1c48a3467581ec317087d233f9d"
HF_BASE_URL = f"https://huggingface.co/HichTala/draw2/resolve/{HF_REVISION}/onnx"
# file name -> (size in bytes, sha256). LFS hashes from the HF tree API; the
# JSON hash was computed locally and its git blob sha1 matched HF's oid.
MODEL_FILES = {
    "ygo_yolo.onnx": (39016336, "449e0d42cbf8429b170abf44d50cda29beeeb05dc80890102e12c06d6358cf97"),
    "vit_fp32.onnx": (385863800, "b59871f8bf766a0bbbb97756bf4a946d9b92cb044a267641d44932446a6b3c72"),
    "cardnames_onnx.json": (2233213, "edd7802f759c3484dff8be5cb2e8be83da2819c5ba6296e0929fd4b5aa00fcc5"),
}

YOLO_SIZE = 640
VIT_SIZE = 224
TOP_K = 3
NMS_IOU_THRESHOLD = 0.5
# Boxes from the 90/180/270-degree detector passes are added only where they
# cover at most this fraction of the smaller of themselves and every kept box.
# Upright boxes are never replaced; this blocks re-detections and partial boxes
# inside a card, while touching neighbours (near-zero overlap) still get in.
ROTATED_PASS_MAX_OVERLAP = 0.3
# Flip a crop only when the flipped top-1 probability is this many times higher.
ORIENTATION_FLIP_MARGIN = 1.5
CARDS_PER_CLASSIFIER_RUN = 4  # each card is classified in 2 orientations
CROP_MAX_HEIGHT = 400

# Work limits. Inputs whose longest side exceeds MAX_WORKING_SIDE are
# downscaled before any processing (polygons are still reported in original
# image coordinates). The detector sees the whole image as one 640x640
# letterbox: a single pass found all 90 cards of a 15x6 grid at 1946x1102, and
# tiling only added a false positive, so no tiling is done. It does run once
# per 90-degree rotation (see detect_all_orientations): on a tightly packed
# photo, the two cards touching the top edge were found only by rotated passes.
# More than MAX_DETECTIONS boxes (after merging the passes) raises
# RecognitionLimitError instead of classifying them (~0.6 s per card on an
# 8-core CPU).
MAX_WORKING_SIDE = 4096
MAX_DETECTIONS = 200
# ONNX Runtime intra-op threads, so recognition leaves CPU for the UI.
ORT_THREADS = max(1, (os.cpu_count() or 2) // 2)


class RecognitionLimitError(ValueError):
    """The input exceeds a documented work limit."""


def models_ready(model_dir=MODEL_DIR):
    """True when every model file exists with the expected size.

    Hashes are verified by download_models, not here, to keep this cheap.
    """
    model_dir = Path(model_dir)
    for name, (size, _) in MODEL_FILES.items():
        path = model_dir / name
        if not path.is_file() or path.stat().st_size != size:
            return False
    return True


def _sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def download_models(model_dir=MODEL_DIR, progress=None):
    """Download the pinned model files, verifying size and sha256.

    Files already present with the right hash are kept. Each file is written to
    a .part file and moved into place only after verification. Any failure
    raises RuntimeError naming the file and URL.
    """
    report = progress or (lambda message: None)
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)

    for name, (expected_size, expected_sha256) in MODEL_FILES.items():
        path = model_dir / name
        if path.is_file() and path.stat().st_size == expected_size:
            report(f"Verifying existing {name}")
            if _sha256_of(path) == expected_sha256:
                report(f"{name} already present and verified")
                continue
            report(f"{name} has the wrong sha256; downloading it again")

        url = f"{HF_BASE_URL}/{name}"
        part_path = path.with_name(name + ".part")
        try:
            _download_file(url, part_path, expected_size, expected_sha256, name, report)
            os.replace(part_path, path)
        except Exception as error:
            part_path.unlink(missing_ok=True)
            raise RuntimeError(f"Downloading {name} from {url} failed: {error}") from error
        report(f"{name} downloaded and verified")


def _download_file(url, part_path, expected_size, expected_sha256, name, report):
    digest = hashlib.sha256()
    received = 0
    last_reported_percent = -1
    with urllib.request.urlopen(url, timeout=60) as response, open(part_path, "wb") as file:
        for block in iter(lambda: response.read(1 << 20), b""):
            file.write(block)
            digest.update(block)
            received += len(block)
            percent = received * 100 // expected_size
            if percent // 5 != last_reported_percent // 5:
                last_reported_percent = percent
                report(f"Downloading {name}: {percent}% ({received // 1_000_000} MB)")
    if received != expected_size:
        raise RuntimeError(f"size mismatch: got {received} bytes, expected {expected_size}")
    actual_sha256 = digest.hexdigest()
    if actual_sha256 != expected_sha256:
        raise RuntimeError(f"sha256 mismatch: got {actual_sha256}, expected {expected_sha256}")


# ---------------------------------------------------------------- geometry

def box_to_portrait_polygon(cx, cy, width, height, angle):
    """Corners (TL, TR, BR, BL in the card's frame) of a rotated box, with the
    long side forced vertical so the card is upright or upside down, never
    sideways. Of the two portrait orientations, the one whose top edge is
    higher in the image is returned (most photos are taken upright)."""
    if width > height:
        width, height = height, width
        angle += math.pi / 2
    # The card's "down" direction in image coords is (-sin, cos).
    if math.cos(angle) < 0:
        angle += math.pi
    cos, sin = math.cos(angle), math.sin(angle)
    half_w, half_h = width / 2, height / 2
    corners = [(-half_w, -half_h), (half_w, -half_h), (half_w, half_h), (-half_w, half_h)]
    return np.array(
        [[cx + dx * cos - dy * sin, cy + dx * sin + dy * cos] for dx, dy in corners],
        dtype=np.float32,
    )


def rotated_iou(polygon_a, polygon_b):
    """Exact intersection over union of two convex polygons."""
    a = np.asarray(polygon_a, dtype=np.float32)
    b = np.asarray(polygon_b, dtype=np.float32)
    intersection, _ = cv2.intersectConvexConvex(a, b)
    if intersection <= 0:
        return 0.0
    return intersection / (abs(cv2.contourArea(a)) + abs(cv2.contourArea(b)) - intersection)


def rotated_nms(detections):
    """Keep the highest-scoring box of each overlapping group.

    detections: list of dicts with 'polygon' and 'detection_score'. Separate
    cards (including identical copies side by side) barely overlap, so they
    all survive; merging is purely spatial, never by card identity.
    """
    kept = []
    for candidate in sorted(detections, key=lambda d: d["detection_score"], reverse=True):
        if all(rotated_iou(candidate["polygon"], existing["polygon"]) <= NMS_IOU_THRESHOLD
               for existing in kept):
            kept.append(candidate)
    return kept


def overlap_of_smaller(polygon_a, polygon_b):
    """Intersection area divided by the smaller polygon's area (1.0 = one contains the other)."""
    a = np.asarray(polygon_a, dtype=np.float32)
    b = np.asarray(polygon_b, dtype=np.float32)
    intersection, _ = cv2.intersectConvexConvex(a, b)
    if intersection <= 0:
        return 0.0
    return intersection / min(abs(cv2.contourArea(a)), abs(cv2.contourArea(b)))


def unrotate_points(points, quarter_turns, width, height):
    """Map points found in np.rot90(image, quarter_turns) back to the original
    width x height image (continuous pixel coordinates)."""
    x, y = points[:, 0], points[:, 1]
    if quarter_turns == 1:
        mapped = (width - y, x)
    elif quarter_turns == 2:
        mapped = (width - x, height - y)
    elif quarter_turns == 3:
        mapped = (y, height - x)
    else:
        raise ValueError(f"quarter_turns must be 1, 2 or 3, got {quarter_turns}")
    return np.stack(mapped, axis=1).astype(np.float32)


def portrait_polygon_from_corners(corners):
    """Re-normalise a mapped rectangle (corners in TL, TR, BR, BL order) with
    box_to_portrait_polygon, so it gets the same upright preference as an
    upright-pass box."""
    center = corners.mean(axis=0)
    top_edge = corners[1] - corners[0]
    width = np.linalg.norm(top_edge)
    height = np.linalg.norm(corners[3] - corners[0])
    angle = math.atan2(top_edge[1], top_edge[0])
    return box_to_portrait_polygon(center[0], center[1], width, height, angle)


def reading_order(detections):
    """Sort into rows (top to bottom), then left to right within a row."""
    if not detections:
        return []
    centers = [d["polygon"].mean(axis=0) for d in detections]
    heights = [np.linalg.norm(d["polygon"][3] - d["polygon"][0]) for d in detections]
    row_gap = 0.5 * float(np.median(heights))
    by_y = sorted(range(len(detections)), key=lambda i: centers[i][1])
    rows = [[by_y[0]]]
    for i in by_y[1:]:
        if centers[i][1] - centers[rows[-1][-1]][1] > row_gap:
            rows.append([])
        rows[-1].append(i)
    return [detections[i] for row in rows for i in sorted(row, key=lambda i: centers[i][0])]


def warp_card(rgb, polygon):
    """Perspective-correct the card into a portrait crop (polygon[0] at top-left)."""
    card_width = np.linalg.norm(polygon[1] - polygon[0])
    card_height = np.linalg.norm(polygon[3] - polygon[0])
    crop_height = int(min(CROP_MAX_HEIGHT, max(VIT_SIZE, card_height)))
    crop_width = max(1, round(crop_height * card_width / card_height))
    target = np.array([[0, 0], [crop_width, 0], [crop_width, crop_height], [0, crop_height]], dtype=np.float32)
    transform = cv2.getPerspectiveTransform(np.asarray(polygon, dtype=np.float32), target)
    return cv2.warpPerspective(rgb, transform, (crop_width, crop_height), flags=cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))


def softmax(logits):
    shifted = np.exp(logits - logits.max())
    return shifted / shifted.sum()


def pick_orientation(probabilities_as_detected, probabilities_flipped):
    """0 keeps the image-upright box orientation, 180 flips it.

    On clean scans the classifier is close to rotation-invariant (both
    orientations score ~0.9 for the same card), so a plain "higher top-1 wins"
    flips upright cards on noise. Flip only when the flipped crop's top-1
    probability is clearly higher; on real photos a wrong orientation
    typically scores 10x lower.
    """
    if probabilities_flipped.max() > ORIENTATION_FLIP_MARGIN * probabilities_as_detected.max():
        return 180
    return 0


# ---------------------------------------------------------------- inference

class Recognizer:
    def __init__(self, model_dir=MODEL_DIR):
        model_dir = Path(model_dir)
        if not models_ready(model_dir):
            raise FileNotFoundError(
                f"DRAW2 model files missing or incomplete in {model_dir.resolve()}; "
                "run download_models() first")
        # Same-size corruption or a swapped label file would silently remap
        # every prediction, so hashes are checked on every load (~1-2 s).
        for name, (_, expected_sha256) in MODEL_FILES.items():
            actual_sha256 = _sha256_of(model_dir / name)
            if actual_sha256 != expected_sha256:
                raise RuntimeError(
                    f"{model_dir / name} has sha256 {actual_sha256}, expected {expected_sha256}; "
                    "delete it and run download_models() again")

        options = ort.SessionOptions()
        options.intra_op_num_threads = ORT_THREADS
        providers = ["CPUExecutionProvider"]
        self.detector = ort.InferenceSession(str(model_dir / "ygo_yolo.onnx"), options, providers=providers)
        self.classifier = ort.InferenceSession(str(model_dir / "vit_fp32.onnx"), options, providers=providers)
        with open(model_dir / "cardnames_onnx.json", encoding="utf-8") as file:
            self.labels = json.load(file)

        class_count = self.classifier.get_outputs()[0].shape[-1]
        if sorted(self.labels, key=int) != [str(i) for i in range(class_count)]:
            raise RuntimeError(
                f"Label file keys are not exactly 0..{class_count - 1} for the classifier's {class_count} outputs")
        self.detector_input = self.detector.get_inputs()[0].name
        self.classifier_input = self.classifier.get_inputs()[0].name

    def recognize(self, image, detection_threshold=0.25, recognition_threshold=0.5, progress=None):
        for name, value in (("detection_threshold", detection_threshold),
                            ("recognition_threshold", recognition_threshold)):
            if not 0.0 <= value <= 1.0:  # also rejects NaN
                raise ValueError(f"{name} must be within [0, 1], got {value}")
        report = progress or (lambda message: None)
        started = time.perf_counter()
        rgb = np.asarray(image.convert("RGB"))
        original_height, original_width = rgb.shape[:2]
        working_scale = min(1.0, MAX_WORKING_SIDE / max(original_width, original_height))
        if working_scale < 1.0:
            report(f"Downscaling {original_width}x{original_height} to at most {MAX_WORKING_SIDE} px")
            rgb = cv2.resize(rgb, (round(original_width * working_scale), round(original_height * working_scale)),
                             interpolation=cv2.INTER_AREA)

        report("Detecting cards")
        boxes = reading_order(self.detect_all_orientations(rgb, detection_threshold))
        if len(boxes) > MAX_DETECTIONS:
            raise RecognitionLimitError(
                f"{len(boxes)} cards detected; the limit is {MAX_DETECTIONS} per image")
        report(f"Detected {len(boxes)} cards")

        crops = [warp_card(rgb, box["polygon"]) for box in boxes]
        results = []
        for start in range(0, len(boxes), CARDS_PER_CLASSIFIER_RUN):
            chunk = range(start, min(start + CARDS_PER_CLASSIFIER_RUN, len(boxes)))
            report(f"Classifying cards {chunk.start + 1}-{chunk.stop} of {len(boxes)}")
            images = []
            for i in chunk:
                square = cv2.resize(crops[i], (VIT_SIZE, VIT_SIZE), interpolation=cv2.INTER_AREA)
                normalized = (square.astype(np.float32) / 255.0 - 0.5) / 0.5
                images += [normalized, normalized[::-1, ::-1]]
            batch = np.ascontiguousarray(np.stack(images).transpose(0, 3, 1, 2))
            logits = self.classifier.run(None, {self.classifier_input: batch})[0].astype(np.float64)
            if logits.shape != (len(images), len(self.labels)) or not np.isfinite(logits).all():
                raise RuntimeError(
                    f"Classifier returned shape {logits.shape} (expected {(len(images), len(self.labels))}) "
                    "or non-finite logits")
            for offset, i in enumerate(chunk):
                as_detected = softmax(logits[2 * offset])
                flipped = softmax(logits[2 * offset + 1])
                results.append(self._result(i + 1, boxes[i], crops[i], as_detected, flipped,
                                            working_scale, recognition_threshold))
        report(f"Recognized {len(results)} cards in {time.perf_counter() - started:.1f}s")
        return results

    def detect_all_orientations(self, rgb, detection_threshold):
        """Upright detection plus boxes that only the 90/180/270-degree passes find.

        Upright boxes (after NMS) are kept exactly as detected. Rotated-pass
        boxes are mapped back to rgb coordinates, NMS-ed among themselves, then
        added in score order only if they overlap no kept box (including ones
        added before them) by more than ROTATED_PASS_MAX_OVERLAP.
        """
        height, width = rgb.shape[:2]
        kept = rotated_nms(self._detect(rgb, detection_threshold))
        rotated_boxes = []
        for quarter_turns in (1, 2, 3):
            rotated = np.ascontiguousarray(np.rot90(rgb, quarter_turns))
            for box in self._detect(rotated, detection_threshold):
                corners = unrotate_points(box["polygon"], quarter_turns, width, height)
                rotated_boxes.append({"polygon": portrait_polygon_from_corners(corners),
                                      "detection_score": box["detection_score"]})
        for candidate in rotated_nms(rotated_boxes):
            if all(overlap_of_smaller(candidate["polygon"], existing["polygon"]) <= ROTATED_PASS_MAX_OVERLAP
                   for existing in kept):
                kept.append(candidate)
        return kept

    def _detect(self, rgb, detection_threshold):
        """Letterbox to 640, run the oriented detector, return portrait boxes in rgb coords."""
        height, width = rgb.shape[:2]
        scale = min(YOLO_SIZE / width, YOLO_SIZE / height)
        new_width, new_height = round(width * scale), round(height * scale)
        pad_x, pad_y = (YOLO_SIZE - new_width) // 2, (YOLO_SIZE - new_height) // 2
        canvas = np.zeros((YOLO_SIZE, YOLO_SIZE, 3), dtype=np.uint8)
        canvas[pad_y:pad_y + new_height, pad_x:pad_x + new_width] = cv2.resize(
            rgb, (new_width, new_height), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
        tensor = np.ascontiguousarray((canvas.astype(np.float32) / 255.0).transpose(2, 0, 1)[None])

        output = self.detector.run(None, {self.detector_input: tensor})[0]
        if output.ndim != 3 or output.shape[1] != 6:
            raise RuntimeError(f"Unexpected detector output shape {output.shape}, expected [1, 6, N]")
        if not np.isfinite(output).all():
            raise RuntimeError("Detector returned non-finite values")
        center_x, center_y, box_w, box_h, score, angle = output[0]
        boxes = []
        for i in np.nonzero(score >= detection_threshold)[0]:
            if box_w[i] <= 0 or box_h[i] <= 0:
                raise RuntimeError(f"Detector returned a box with non-positive size: {output[0][:, i]}")
            polygon = box_to_portrait_polygon(
                (center_x[i] - pad_x) / scale, (center_y[i] - pad_y) / scale,
                box_w[i] / scale, box_h[i] / scale, float(angle[i]))
            boxes.append({"polygon": polygon, "detection_score": float(score[i])})
        return boxes

    def _result(self, number, box, crop, as_detected, flipped, working_scale, recognition_threshold):
        polygon = box["polygon"]
        probabilities = as_detected
        if pick_orientation(as_detected, flipped) == 180:
            probabilities = flipped
            crop = crop[::-1, ::-1]
            polygon = polygon[[2, 3, 0, 1]]
        candidates = []
        for index in np.argsort(probabilities)[::-1][:TOP_K]:
            label = self.labels[str(index)]
            candidates.append({
                "card_id": label["card_id"],
                "name_en": label["EN"],
                "name_ja": label.get("JA"),
                "score": float(probabilities[index]),
            })
        return {
            "index": number,
            "polygon": (polygon / working_scale).round(1).tolist(),
            "crop": Image.fromarray(np.ascontiguousarray(crop)),
            "candidates": candidates,
            "status": "recognized" if candidates[0]["score"] >= recognition_threshold else "review",
            "detection_score": box["detection_score"],
        }
