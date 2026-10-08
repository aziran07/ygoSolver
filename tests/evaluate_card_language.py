"""Opt-in OCR evaluation against manually labeled, private local images.

Run from repo root: python tests/evaluate_card_language.py MANIFEST --output OUTPUT
Manifest entries: path, locale (ko/ja), kind (photo/official). Photo labels apply
to every card in a single-edition photo. Official images use their full bounds.
Image assets and reports stay outside Git. Labels must be reviewed independently.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import card_language
import recognition


def evaluate(manifest, output):
    entries = json.loads(Path(manifest).read_text(encoding="utf-8"))
    report = {"module_sha256": hashlib.sha256(Path(card_language.__file__).read_bytes()).hexdigest(),
              "models": {}, "images": [], "summary": {}}
    for role, (name, _, _, expected_hash) in card_language.OCR_MODELS.items():
        model = card_language.MODEL_DIR / name
        actual_hash = hashlib.sha256(model.read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            raise ValueError(f"Evaluation model checksum mismatch: {model}")
        report["models"][role] = {"name": name, "sha256": actual_hash}
    recognizer = None
    groups = defaultdict(list)
    crop_dir = output.parent / "crops"
    crop_dir.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        if entry["locale"] not in ("ko", "ja") or entry["kind"] not in ("photo", "official"):
            raise ValueError(f"Invalid ground-truth entry: {entry}")
        image = card_language.load_image(entry["path"])
        started = time.perf_counter()
        if entry["kind"] == "photo":
            if recognizer is None:
                recognizer = recognition.Recognizer()
            detections = recognizer.recognize(image)
        else:
            detections = [{"index": 1, "polygon": [[0, 0], [image.width, 0],
                           [image.width, image.height], [0, image.height]]}]
        item = {**entry, "size": image.size, "detection_seconds": time.perf_counter() - started, "cards": []}
        for detection in detections:
            result = card_language.detect_language(image, detection["polygon"])
            verdict = "review" if result["locale"] is None else (
                "correct" if result["locale"] == entry["locale"] else "wrong")
            crop_path = crop_dir / f"{Path(entry['path']).stem}_{detection['index']}.png"
            card_language.extract_card(image, detection["polygon"]).save(crop_path)
            card = {"index": detection["index"], "polygon": detection["polygon"],
                    "expected": entry["locale"], "verdict": verdict, "crop_path": str(crop_path), **result}
            item["cards"].append(card)
            groups[f"{entry['kind']}_{entry['locale']}"].append(card)
        report["images"].append(item)
        print(entry["path"], dict(Counter(c["verdict"] for c in item["cards"])), flush=True)
    for group, cards in groups.items():
        counts = Counter(c["verdict"] for c in cards)
        report["summary"][group] = {"total": len(cards), **{k: counts[k] for k in ("correct", "wrong", "review")},
                                  "median_seconds": statistics.median(c["elapsed_seconds"] for c in cards),
                                  "total_seconds": sum(c["elapsed_seconds"] for c in cards)}
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    evaluate(args.manifest, args.output)
