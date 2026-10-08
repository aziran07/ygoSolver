"""Korean/Japanese card-language inference with PaddleOCR ONNX models.

Pipeline per recognized card: perspective-warp the card from the original
image at its native resolution (extract_card), find text lines with the
PP-OCRv5 mobile text detector, keep the lines in the title and text-box bands,
and read every line with two recognizers whose character sets do not overlap
in the scripts that matter: the PP-OCRv5 Korean model has Hangul and no kana,
the PP-OCRv6 multilingual model has kana and Han and no Hangul. Each script is
therefore only ever produced by the model that can read it (classify_characters).

Only Korean ('ko') and Japanese ('ja') are classes. No confident Hangul or
kana, kanji-only text, or confident evidence for both is 'review'. Character
probabilities are the recognizer's own CTC posteriors, not calibrated language
probabilities. Operational failures (missing or corrupt model files,
unexpected model outputs) raise LanguageDetectionError; they are never turned
into a 'review' result.

Models: PaddleOCR (Apache-2.0) ONNX conversions published by RapidOCR v3.10.0,
pinned below by size and SHA-256 and downloaded into data/models
(or $OCR_MODEL_DIR).

CLI:
    python card_language.py --download-models [--model-dir DIR]
    python card_language.py IMAGE... --output OUTPUT_JSON [--save-crops DIR] [--model-dir DIR]
"""

import argparse
import functools
import json
import math
import os
import time
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort
from PIL import Image, ImageOps

import recognition

# OCR_MODEL_DIR overrides the directory, e.g. for a read-only data/models mount.
MODEL_DIR = Path(os.environ.get("OCR_MODEL_DIR", "data/models"))
MODEL_BASE_URL = "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx"
# role -> (file name, path under MODEL_BASE_URL, size in bytes, sha256). Sizes
# and hashes match RapidOCR 3.10.0 default_models.yaml.
OCR_MODELS = {
    "detector": ("ch_PP-OCRv5_det_mobile.onnx", "PP-OCRv5/det", 4819576,
                 "4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae"),
    "ko": ("korean_PP-OCRv5_rec_mobile.onnx", "PP-OCRv5/rec", 13488748,
           "cd6e2ea50f6943ca7271eb8c56a877a5a90720b7047fe9c41a2e541a25773c9b"),
    "ja": ("PP-OCRv6_rec_small.onnx", "PP-OCRv6/rec", 21234383,
           "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884"),
}

MIN_CARD_SIDE = 16  # px in the original image; smaller polygons are rejected
# The card is resized to this height for text detection (upscaled at most
# MAX_UPSCALE times). On the development photos 960 read more cards than 1280
# or 1600, where blurred upscaled cards produced more false lines.
DETECTION_CARD_HEIGHT = 960
MAX_UPSCALE = 4.0
# Text lines whose center lies in these vertical bands (fractions of the card
# height) are read: the card name with its furigana, and the text box (type
# line, effect text, ATK/DEF). The bands are wider than the printed areas
# because detected card outlines are often loose.
TEXT_BANDS = {"title": (0.0, 0.17), "text_box": (0.66, 0.98)}
# PP-OCR pre/post-processing as in RapidOCR 3.10.0 (config.yaml defaults and
# ch_ppocr_det / ch_ppocr_rec code): BGR input normalised with mean = std = 0.5;
# the detector input's short side is enlarged to at least 736 px and rounded to
# multiples of 32; pixels above 0.3 (dilated 2x2) form regions whose rotated box
# needs mean probability 0.5 and a short side of 3 px, and boxes are grown by
# the DB unclip ratio. Recognizer input is 48 px high, at least 320 px wide,
# zero-padded on the right.
DETECTION_MIN_SIDE = 736
DETECTION_PIXEL_THRESHOLD = 0.3
DETECTION_BOX_THRESHOLD = 0.5
DETECTION_UNCLIP_RATIO = 1.6
DETECTION_MIN_BOX_SIDE = 3
RECOGNITION_HEIGHT = 48
RECOGNITION_MIN_WIDTH = 320
PROBABILITY_TOLERANCE = 1e-5
# Each recognizer time step is a softmax over all classes; float32 sums drift a little.
RECOGNITION_SUM_TOLERANCE = 1e-3

# Classification. A decoded character counts only when the recognizer's
# probability for it is at least MIN_CHARACTER_PROBABILITY (the argmax then
# holds most of the probability mass; 0.5 is also RapidOCR's text_score
# default). A language needs MIN_SCRIPT_CHARS such characters and none of the
# other script. On the development set every wrong-script character was
# below 0.32 while the median own-script character was above 0.95.
MIN_CHARACTER_PROBABILITY = 0.5
MIN_SCRIPT_CHARS = 2


class LanguageDetectionError(RuntimeError):
    """OCR could not run or returned unusable output; the cause is in the message."""


def char_script(char):
    code = ord(char)
    if 0xAC00 <= code <= 0xD7A3 or 0x1100 <= code <= 0x11FF or 0x3130 <= code <= 0x318F:
        return "hangul"
    # Kana without the middle dots (U+30FB, U+FF65) and prolonged sound marks
    # (U+30FC, U+FF70), which also stand for dots and dashes outside Japanese text.
    if 0x3041 <= code <= 0x309F or 0x30A0 <= code <= 0x30FA or 0x30FD <= code <= 0x30FF             or 0xFF66 <= code <= 0xFF6F or 0xFF71 <= code <= 0xFF9D:
        return "kana"
    if 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF:
        return "han"
    if char.isascii() and char.isalpha():
        return "latin"
    if char.isdigit():
        return "digit"
    return "other"


def _validated_characters(characters, reader):
    if not isinstance(characters, list):
        raise ValueError(f"{reader} characters must be a list, got {type(characters).__name__}")
    for position, item in enumerate(characters):
        if not isinstance(item, dict):
            raise ValueError(f"{reader} character {position} must be a dict, got {type(item).__name__}")
        text, probability = item.get("text"), item.get("probability")
        if not isinstance(text, str) or len(text) != 1:
            raise ValueError(f"{reader} character {position} text must be one character, got {text!r}")
        if isinstance(probability, bool) or not isinstance(probability, (int, float)) \
                or not 0 <= probability <= 1:  # also rejects NaN
            raise ValueError(f"{reader} character {position} probability must be in [0, 1], got {probability!r}")
    return characters


def classify_characters(korean, japanese):
    """Classify the two recognizers' readings as {'locale', 'status', 'reason', 'evidence'}.

    korean: characters read by the Korean model, japanese: by the Japanese
    model; each a list of {'text': one character, 'probability': float in [0, 1]}.
    Only Hangul from the Korean reading and kana from the Japanese reading count.
    """
    _validated_characters(korean, "korean")
    _validated_characters(japanese, "japanese")
    hangul = sum(char_script(c["text"]) == "hangul" and c["probability"] >= MIN_CHARACTER_PROBABILITY for c in korean)
    kana = sum(char_script(c["text"]) == "kana" and c["probability"] >= MIN_CHARACTER_PROBABILITY for c in japanese)
    evidence = {"hangul": hangul, "kana": kana, "min_character_probability": MIN_CHARACTER_PROBABILITY,
                "min_script_chars": MIN_SCRIPT_CHARS}
    # User-facing reasons stay short; thresholds and counts are in the evidence.
    if hangul >= MIN_SCRIPT_CHARS and kana == 0:
        locale, reason = "ko", f"카드 글자에서 한글을 읽었습니다 (한글 {hangul}자)"
    elif kana >= MIN_SCRIPT_CHARS and hangul == 0:
        locale, reason = "ja", f"카드 글자에서 일본어 가나를 읽었습니다 (가나 {kana}자)"
    elif hangul and kana:
        locale, reason = None, "한글과 가나가 모두 읽혀 판본을 정하지 못했습니다"
    else:
        locale, reason = None, "한글이나 가나를 충분히 읽지 못했습니다 (사진 속 카드가 작거나 흐리면 흔함)"
    return {"locale": locale, "status": "review" if locale is None else "classified",
            "reason": reason, "evidence": evidence}


# ---------------------------------------------------------------- models

def models_ready(model_dir=MODEL_DIR):
    """True when every OCR model file exists with the expected size (hashes are checked on load)."""
    model_dir = Path(model_dir)
    return all((model_dir / name).is_file() and (model_dir / name).stat().st_size == size
               for name, _, size, _ in OCR_MODELS.values())


def download_models(model_dir=MODEL_DIR, progress=None):
    """Download the pinned OCR models, verifying size and sha256 (same scheme as recognition.download_models)."""
    report = progress or (lambda message: None)
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    for name, url_path, expected_size, expected_sha256 in OCR_MODELS.values():
        path = model_dir / name
        if path.is_file() and path.stat().st_size == expected_size:
            if recognition._sha256_of(path) == expected_sha256:
                report(f"{name} already present and verified")
                continue
            report(f"{name} has the wrong sha256; downloading it again")
        url = f"{MODEL_BASE_URL}/{url_path}/{name}"
        part_path = path.with_name(name + ".part")
        try:
            recognition._download_file(url, part_path, expected_size, expected_sha256, name, report)
            os.replace(part_path, path)
        except Exception as error:
            part_path.unlink(missing_ok=True)
            raise RuntimeError(f"Downloading {name} from {url} failed: {error}") from error
        report(f"{name} downloaded and verified")


@functools.lru_cache(maxsize=None)
def load_models(model_dir=MODEL_DIR):
    """{'detector': session, 'ko'/'ja': (session, characters)}; LanguageDetectionError when unusable.

    Failures are not cached, so a later call retries after the models are installed.
    """
    model_dir = Path(model_dir)
    options = ort.SessionOptions()
    options.intra_op_num_threads = recognition.ORT_THREADS
    models = {}
    for role, (name, _, expected_size, expected_sha256) in OCR_MODELS.items():
        path = model_dir / name
        if not path.is_file() or path.stat().st_size != expected_size:
            raise LanguageDetectionError(
                f"OCR model {path.resolve()} is missing or not {expected_size} bytes; "
                "run `python card_language.py --download-models`")
        actual_sha256 = recognition._sha256_of(path)
        if actual_sha256 != expected_sha256:
            raise LanguageDetectionError(
                f"OCR model {path.resolve()} has sha256 {actual_sha256}, expected {expected_sha256}; "
                "delete it and run `python card_language.py --download-models`")
        try:
            session = ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])
        except Exception as error:
            raise LanguageDetectionError(f"ONNX Runtime could not load {path}: {error}") from error
        if role == "detector":
            models[role] = session
            continue
        metadata = session.get_modelmeta().custom_metadata_map
        if not metadata.get("character"):
            raise LanguageDetectionError(f"Recognizer {path} has no 'character' metadata")
        # CTC classes: blank, the model's characters, then the space character.
        characters = [None, *metadata["character"].splitlines(), " "]
        class_count = session.get_outputs()[0].shape[-1]
        if class_count != len(characters):
            raise LanguageDetectionError(
                f"Recognizer {path} has {class_count} output classes but {len(characters)} characters")
        models[role] = (session, characters)
    return models


# ---------------------------------------------------------------- geometry

def _validated_polygon(polygon):
    try:
        points = np.asarray(polygon, dtype=np.float64)
    except (TypeError, ValueError) as error:
        raise ValueError(f"polygon must be 4 numeric (x, y) points, got {polygon!r}") from error
    if points.shape != (4, 2) or not np.isfinite(points).all():
        raise ValueError(f"polygon must be 4 finite (x, y) points, got {polygon!r}")
    sides = [float(np.linalg.norm(points[(i + 1) % 4] - points[i])) for i in range(4)]
    if min(sides) < MIN_CARD_SIDE:
        raise ValueError(f"polygon side lengths {[round(s, 1) for s in sides]} include one under {MIN_CARD_SIDE} px")
    if not cv2.isContourConvex(points.astype(np.float32)):
        raise ValueError(f"polygon is not a convex quadrilateral: {points.tolist()}")
    # TL, TR, BR, BL runs clockwise on screen (positive shoelace area with y down);
    # the reverse order would produce a mirrored crop.
    x, y = points[:, 0], points[:, 1]
    if np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y) <= 0:
        raise ValueError(f"polygon corners must be ordered TL, TR, BR, BL (clockwise on screen): {points.tolist()}")
    return points


def extract_card(image, polygon):
    """Upright RGB card crop at the polygon's native resolution.

    polygon: TL, TR, BR, BL corners of the card in original image coordinates,
    already oriented (as in Recognizer.recognize results).
    """
    points = _validated_polygon(polygon)
    width = round((np.linalg.norm(points[1] - points[0]) + np.linalg.norm(points[2] - points[3])) / 2)
    height = round((np.linalg.norm(points[3] - points[0]) + np.linalg.norm(points[2] - points[1])) / 2)
    target = np.array([[0, 0], [width, 0], [width, height], [0, height]], dtype=np.float32)
    transform = cv2.getPerspectiveTransform(points.astype(np.float32), target)
    rgb = np.asarray(image.convert("RGB"))
    card = cv2.warpPerspective(rgb, transform, (width, height), flags=cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))
    return Image.fromarray(card)


# ---------------------------------------------------------------- OCR

def _run(session, tensor, stage):
    try:
        return session.run(None, {session.get_inputs()[0].name: tensor})[0]
    except Exception as error:
        raise LanguageDetectionError(f"{stage} failed in ONNX Runtime: {type(error).__name__}: {error}") from error


def _check_probabilities(output, expected, stage):
    """Raise unless output matches the expected shape (None = any positive size) and holds probabilities."""
    shape_ok = output.ndim == len(expected) and all(
        size > 0 if want is None else size == want for size, want in zip(output.shape, expected))
    if not shape_ok:
        raise LanguageDetectionError(f"{stage} returned shape {output.shape}, expected {expected}")
    # Sigmoid/softmax outputs in float32 can exceed 1 by rounding (1.0000001 seen); allow that much only.
    if not np.isfinite(output).all() or output.min() < -PROBABILITY_TOLERANCE or output.max() > 1 + PROBABILITY_TOLERANCE:
        raise LanguageDetectionError(
            f"{stage} returned values outside [0, 1] or non-finite (min {output.min()}, max {output.max()})")


def _ordered_box(rect):
    """Corners of a cv2 rotated rect as TL, TR, BR, BL (RapidOCR get_mini_boxes ordering)."""
    points = sorted(cv2.boxPoints(rect).tolist(), key=lambda point: point[0])
    left = sorted(points[:2], key=lambda point: point[1])
    right = sorted(points[2:], key=lambda point: point[1])
    return np.array([left[0], right[0], right[1], left[1]], dtype=np.float32)


def detect_text_lines(detector, bgr):
    """Text line boxes [(corners TL, TR, BR, BL as 4x2 float32, score)] in bgr coordinates, top to bottom."""
    height, width = bgr.shape[:2]
    ratio = max(1.0, DETECTION_MIN_SIDE / min(height, width))
    input_height = max(32, round(int(height * ratio) / 32) * 32)
    input_width = max(32, round(int(width * ratio) / 32) * 32)
    resized = cv2.resize(bgr, (input_width, input_height)).astype(np.float32)
    tensor = np.ascontiguousarray(((resized / 255.0 - 0.5) / 0.5).transpose(2, 0, 1)[None])
    output = _run(detector, tensor, "Text detector")
    _check_probabilities(output, (1, 1, input_height, input_width), "Text detector")
    probability = output[0, 0]
    mask = cv2.dilate((probability > DETECTION_PIXEL_THRESHOLD).astype(np.uint8), np.ones((2, 2), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    lines = []
    for contour in contours:
        center, (rect_width, rect_height), angle = cv2.minAreaRect(contour)
        if min(rect_width, rect_height) < DETECTION_MIN_BOX_SIDE:
            continue
        box = _ordered_box((center, (rect_width, rect_height), angle))
        box_mask = np.zeros(probability.shape, dtype=np.uint8)
        cv2.fillPoly(box_mask, [box.round().astype(np.int32)], 1)
        score = float(cv2.mean(probability, box_mask)[0])
        if score < DETECTION_BOX_THRESHOLD:
            continue
        # Offsetting a rectangle outward by d (pyclipper in RapidOCR) grows each side by 2d.
        grow = rect_width * rect_height * DETECTION_UNCLIP_RATIO / (2 * (rect_width + rect_height))
        grown_size = (rect_width + 2 * grow, rect_height + 2 * grow)
        if min(grown_size) < DETECTION_MIN_BOX_SIDE + 2:
            continue
        box = _ordered_box((center, grown_size, angle))
        box[:, 0] = np.clip(np.round(box[:, 0] / input_width * width), 0, width)
        box[:, 1] = np.clip(np.round(box[:, 1] / input_height * height), 0, height)
        lines.append((box, score))
    return sorted(lines, key=lambda line: (line[0][:, 1].min(), line[0][:, 0].min()))


def crop_text_line(bgr, box):
    """Perspective crop of a detected line; tall crops are turned upright (RapidOCR get_rotate_crop_image)."""
    crop_width = max(1, int(max(np.linalg.norm(box[0] - box[1]), np.linalg.norm(box[2] - box[3]))))
    crop_height = max(1, int(max(np.linalg.norm(box[0] - box[3]), np.linalg.norm(box[1] - box[2]))))
    target = np.array([[0, 0], [crop_width, 0], [crop_width, crop_height], [0, crop_height]], dtype=np.float32)
    transform = cv2.getPerspectiveTransform(box.astype(np.float32), target)
    line = cv2.warpPerspective(bgr, transform, (crop_width, crop_height),
                               borderMode=cv2.BORDER_REPLICATE, flags=cv2.INTER_CUBIC)
    if crop_height / crop_width >= 1.5:
        line = np.rot90(line)
    return np.ascontiguousarray(line)


def recognize_line(recognizer, bgr):
    """Greedy CTC reading of one BGR text line: [{'text': character, 'probability': float}]."""
    session, characters = recognizer
    height, width = bgr.shape[:2]
    input_width = int(RECOGNITION_HEIGHT * max(RECOGNITION_MIN_WIDTH / RECOGNITION_HEIGHT, width / height))
    resized_width = min(input_width, math.ceil(RECOGNITION_HEIGHT * width / height))
    resized = cv2.resize(bgr, (resized_width, RECOGNITION_HEIGHT)).astype(np.float32)
    tensor = np.zeros((1, 3, RECOGNITION_HEIGHT, input_width), dtype=np.float32)
    tensor[0, :, :, :resized_width] = ((resized / 255.0 - 0.5) / 0.5).transpose(2, 0, 1)
    output = _run(session, tensor, "Text recognizer")
    _check_probabilities(output, (1, None, len(characters)), "Text recognizer")
    step_sums = output[0].sum(axis=1)
    if np.abs(step_sums - 1).max() > RECOGNITION_SUM_TOLERANCE:
        raise LanguageDetectionError(
            f"Text recognizer time steps are not probability distributions (sums {step_sums.min()}..{step_sums.max()})")
    probabilities = output[0]
    reading, previous = [], 0
    for step, index in enumerate(probabilities.argmax(axis=1)):
        if index != 0 and index != previous:
            reading.append({"text": characters[index], "probability": round(float(probabilities[step, index]), 4)})
        previous = index
    return reading


def detect_language(image, polygon, *, model_dir=MODEL_DIR):
    """Language of one recognized card.

    Returns {'locale', 'status', 'reason', 'evidence', 'elapsed_seconds'}; the
    evidence lists every read line with both models' readings.
    """
    started = time.perf_counter()
    models = load_models(Path(model_dir))
    card = extract_card(image, polygon)
    scale = min(MAX_UPSCALE, DETECTION_CARD_HEIGHT / card.height)
    resized = card.resize((max(1, round(card.width * scale)), max(1, round(card.height * scale))),
                          Image.Resampling.BICUBIC)
    bgr = np.ascontiguousarray(np.asarray(resized)[:, :, ::-1])  # the PP-OCR models take BGR, like RapidOCR
    korean, japanese, lines = [], [], []
    for box, score in detect_text_lines(models["detector"], bgr):
        center = box[:, 1].mean() / bgr.shape[0]
        band = next((name for name, (top, bottom) in TEXT_BANDS.items() if top <= center <= bottom), None)
        if band is None:
            continue
        line = crop_text_line(bgr, box)
        korean_reading = recognize_line(models["ko"], line)
        japanese_reading = recognize_line(models["ja"], line)
        korean += korean_reading
        japanese += japanese_reading
        lines.append({"band": band, "box": box.round(1).tolist(), "detection_score": round(score, 4),
                      "korean": "".join(c["text"] for c in korean_reading),
                      "japanese": "".join(c["text"] for c in japanese_reading),
                      "korean_characters": korean_reading, "japanese_characters": japanese_reading})
    result = classify_characters(korean, japanese)
    result["evidence"].update(card_size=[card.width, card.height], detection_scale=round(scale, 4), lines=lines)
    result["elapsed_seconds"] = time.perf_counter() - started
    return result


def load_image(path):
    """Open an image the way app.py does: EXIF orientation applied, RGB."""
    with Image.open(path) as image:
        return ImageOps.exif_transpose(image).convert("RGB")


def main():
    parser = argparse.ArgumentParser(description="Recognize cards in photos and infer each card's language (ko/ja).")
    parser.add_argument("images", nargs="*", type=Path)
    parser.add_argument("--output", type=Path, help="JSON file to write")
    parser.add_argument("--save-crops", type=Path, help="directory for full-resolution card crops (PNG)")
    parser.add_argument("--model-dir", type=Path, default=MODEL_DIR, help=f"OCR model directory (default: {MODEL_DIR})")
    parser.add_argument("--download-models", action="store_true", help="download and verify the OCR models, then exit")
    arguments = parser.parse_args()

    if arguments.download_models:
        if arguments.images:
            parser.error("--download-models takes no images")
        download_models(arguments.model_dir, progress=print)
        return
    if not arguments.images or arguments.output is None:
        parser.error("IMAGE... and --output are required unless --download-models is given")

    load_models(arguments.model_dir)  # fail before the slow recognition if the OCR models are unusable
    recognizer = recognition.Recognizer()
    if arguments.save_crops:
        arguments.save_crops.mkdir(parents=True, exist_ok=True)
    report = {"models": {role: name for role, (name, _, _, _) in OCR_MODELS.items()}, "images": []}
    for path in arguments.images:
        image = load_image(path)
        started = time.perf_counter()
        results = recognizer.recognize(image)
        recognition_seconds = time.perf_counter() - started
        cards = []
        for result in results:
            language = detect_language(image, result["polygon"], model_dir=arguments.model_dir)
            card = {
                "index": result["index"],
                "polygon": result["polygon"],
                "recognition_status": result["status"],
                "top_candidate": {key: result["candidates"][0][key] for key in ("card_id", "name_en", "score")},
                "language": language,
            }
            if arguments.save_crops:
                crop_path = arguments.save_crops / f"{path.stem}_card{result['index']:03d}.png"
                extract_card(image, result["polygon"]).save(crop_path)
                card["crop"] = str(crop_path)
            cards.append(card)
            print(f"{path.name} card {result['index']}: {language['locale'] or 'review'} "
                  f"({language['elapsed_seconds']:.2f}s) {language['reason']}", flush=True)
        report["images"].append({"image": str(path), "size": list(image.size),
                                 "recognition_seconds": recognition_seconds, "cards": cards})
    arguments.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {arguments.output}")


if __name__ == "__main__":
    main()
