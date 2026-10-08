import hashlib
import io
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

import recognition
from recognition import (
    Recognizer, box_to_portrait_polygon, download_models, models_ready, overlap_of_smaller, pick_orientation,
    reading_order, rotated_iou, rotated_nms, softmax, unrotate_points, warp_card,
)


def box(cx, cy, w=60, h=88, angle=0.0, score=0.9):
    return {"polygon": box_to_portrait_polygon(cx, cy, w, h, angle), "detection_score": score}


class GeometryTests(unittest.TestCase):
    def test_landscape_box_becomes_portrait(self):
        polygon = box_to_portrait_polygon(100, 100, 88, 60, 0.0)
        self.assertAlmostEqual(np.linalg.norm(polygon[1] - polygon[0]), 60, places=3)
        self.assertAlmostEqual(np.linalg.norm(polygon[3] - polygon[0]), 88, places=3)

    def test_portrait_polygon_prefers_image_upright(self):
        for angle in [0.0, math.pi, 0.2, math.pi + 0.2, 1.2, -1.2, math.pi - 1.2]:
            with self.subTest(angle=angle):
                polygon = box_to_portrait_polygon(100, 100, 60, 88, angle)
                top_edge_y = (polygon[0][1] + polygon[1][1]) / 2
                bottom_edge_y = (polygon[2][1] + polygon[3][1]) / 2
                self.assertLess(top_edge_y, bottom_edge_y)

    def test_rotated_iou_exact_values(self):
        square = [[0, 0], [10, 0], [10, 10], [0, 10]]
        self.assertAlmostEqual(rotated_iou(square, square), 1.0)
        self.assertEqual(rotated_iou(square, [[20, 0], [30, 0], [30, 10], [20, 10]]), 0.0)
        self.assertAlmostEqual(rotated_iou(square, [[5, 0], [15, 0], [15, 10], [5, 10]]), 50 / 150)
        # A 45-degree rotated copy: axis-aligned IoU would be 0.5; the true
        # overlap is a regular octagon.
        diamond = box_to_portrait_polygon(5, 5, 10, 10, math.pi / 4)
        octagon = 2 * (1 + math.sqrt(2)) * (10 * (math.sqrt(2) - 1)) ** 2
        self.assertAlmostEqual(rotated_iou(square, diamond), octagon / (200 - octagon), places=3)

    def test_nms_merges_double_detection_keeping_best_score(self):
        kept = rotated_nms([box(100, 100, score=0.6), box(102, 101, score=0.9), box(300, 100, score=0.5)])
        self.assertEqual([d["detection_score"] for d in kept], [0.9, 0.5])

    def test_nms_keeps_adjacent_and_crossed_cards(self):
        # Touching copies of the same card, and two tilted cards whose bounding
        # boxes overlap heavily but whose real areas do not.
        self.assertEqual(len(rotated_nms([box(100 + 62 * i, 100) for i in range(5)])), 5)
        tilted = [box(100, 100, 20, 200, math.pi / 4), box(140, 100, 20, 200, math.pi / 4)]
        self.assertEqual(len(rotated_nms(tilted)), 2)

    def test_overlap_of_smaller_detects_containment(self):
        square = [[0, 0], [10, 0], [10, 10], [0, 10]]
        self.assertAlmostEqual(overlap_of_smaller(square, [[2, 2], [5, 2], [5, 5], [2, 5]]), 1.0)
        self.assertAlmostEqual(overlap_of_smaller(square, [[5, 0], [25, 0], [25, 10], [5, 10]]), 0.5)
        self.assertEqual(overlap_of_smaller(square, [[10, 0], [20, 0], [20, 10], [10, 10]]), 0.0)

    def test_unrotate_points_inverts_rot90_on_non_square_image(self):
        width, height, x, y = 85, 49, 20, 7
        image = np.zeros((height, width), dtype=np.uint8)
        image[y, x] = 1
        for quarter_turns in (1, 2, 3):
            with self.subTest(quarter_turns=quarter_turns):
                row, column = np.argwhere(np.rot90(image, quarter_turns))[0]
                pixel_center = np.float32([[column + 0.5, row + 0.5]])
                np.testing.assert_allclose(unrotate_points(pixel_center, quarter_turns, width, height),
                                           [[x + 0.5, y + 0.5]])
        with self.assertRaises(ValueError):
            unrotate_points(pixel_center, 0, width, height)

    def test_reading_order_rows_then_columns(self):
        shuffled = [box(300, 205), box(100, 95), box(200, 100), box(100, 200), box(300, 100)]
        centers = [tuple(np.round(d["polygon"].mean(axis=0))) for d in reading_order(shuffled)]
        self.assertEqual(centers, [(100, 95), (200, 100), (300, 100), (100, 200), (300, 205)])

    def test_warp_card_maps_polygon_to_upright_crop(self):
        image = np.zeros((300, 300, 3), dtype=np.uint8)
        image[50:100, 100:200] = (255, 0, 0)    # red top half of the card
        image[100:150, 100:200] = (0, 0, 255)   # blue bottom half
        polygon = np.array([[100, 50], [200, 50], [200, 150], [100, 150]], dtype=np.float32)
        crop = warp_card(image, polygon)
        self.assertEqual(crop[10, crop.shape[1] // 2].tolist(), [255, 0, 0])
        self.assertEqual(crop[-10, crop.shape[1] // 2].tolist(), [0, 0, 255])
        flipped = warp_card(image, polygon[[2, 3, 0, 1]])
        self.assertEqual(flipped[10, flipped.shape[1] // 2].tolist(), [0, 0, 255])


class ScoringTests(unittest.TestCase):
    def test_softmax_is_always_applied(self):
        # Logits that already look like probabilities still get softmaxed.
        probabilities = softmax(np.array([0.9, 0.1, 0.0]))
        self.assertAlmostEqual(probabilities.sum(), 1.0)
        self.assertAlmostEqual(probabilities[0], math.exp(0.9) / (math.exp(0.9) + math.exp(0.1) + 1))

    def test_orientation_flips_only_on_clear_evidence(self):
        self.assertEqual(pick_orientation(np.array([0.05, 0.95]), np.array([0.5, 0.5])), 0)
        self.assertEqual(pick_orientation(np.array([0.88, 0.12]), np.array([0.92, 0.08])), 0)
        self.assertEqual(pick_orientation(np.array([0.2, 0.1]), np.array([0.9, 0.1])), 180)


# ---------------------------------------------------------------- pipeline with fake models

def detector_output(rows):
    return np.array(rows, dtype=np.float32).reshape(-1, 6).T[None]  # [1, 6, N]


class FakeDetector:
    """Answers the detector passes in recognize's order: upright, then the image
    rotated by 90, 180 and 270 degrees (np.rot90 k = 1, 2, 3). Rotated passes
    find nothing unless rotated_rows gives their rows."""

    def __init__(self, rows, rotated_rows=((), (), ())):
        self.outputs = [detector_output(rows)] + [detector_output(r) for r in rotated_rows]
        self.calls = 0

    def run(self, _outputs, _inputs):
        output = self.outputs[self.calls % 4]
        self.calls += 1
        return [output]


class FakeClassifier:
    """Confident class 0 when the crop's top is red (an upright card), uniform otherwise."""

    def run(self, _outputs, inputs):
        batch = next(iter(inputs.values()))
        top_red = batch[:, 0, :40, :].mean(axis=(1, 2))
        logits = np.zeros((len(batch), 3), dtype=np.float32)
        logits[:, 0] = np.where(top_red > 0, 8.0, 0.0)
        return [logits]


def fake_recognizer(detections, classifier=None, rotated_rows=((), (), ())):
    recognizer = object.__new__(Recognizer)
    recognizer.detector = FakeDetector(detections, rotated_rows)
    recognizer.classifier = classifier or FakeClassifier()
    recognizer.detector_input = recognizer.classifier_input = "x"
    recognizer.labels = {
        "0": {"card_id": "11111111", "EN": "Red Top", "JA": "赤", "label": "Red-Top-11111111"},
        "1": {"card_id": "22222222", "EN": "Blue Top", "label": "Blue-Top-22222222"},
        "2": {"card_id": "33333333", "EN": "Other", "JA": "他", "label": "Other-33333333"},
    }
    return recognizer


def card_image(upside_down_second=False):
    """640x640 image with two cards; each card red on top, blue below."""
    image = np.full((640, 640, 3), 40, dtype=np.uint8)
    for left in (100, 400):
        image[100:200, left:left + 136] = (255, 0, 0)
        image[200:300, left:left + 136] = (0, 0, 255)
    if upside_down_second:
        image[100:300, 400:536] = image[100:300, 400:536][::-1]
    return Image.fromarray(image)


def rotated_pass_row(cx, cy, w, h, score, quarter_turns, image_width, image_height):
    """Detector row (letterbox coords) for an axis-aligned portrait card centred at
    (cx, cy) in the original image, as seen in np.rot90(image, quarter_turns).

    Derived independently of unrotate_points: np.rot90 k=1 sends original pixel
    (x, y) to (y, width - x), so each quarter turn also swaps the card's sides.
    """
    for _ in range(quarter_turns):
        cx, cy, image_width, image_height = cy, image_width - cx, image_height, image_width
        w, h = h, w
    scale = min(640 / image_width, 640 / image_height)
    pad_x = (640 - round(image_width * scale)) // 2
    pad_y = (640 - round(image_height * scale)) // 2
    return [cx * scale + pad_x, cy * scale + pad_y, w * scale, h * scale, score, 0.0]


# Detector rows: cx, cy, w, h, score, angle (640 input == image coords here).
TWO_CARDS = [
    [168, 200, 136, 200, 0.95, 0.0],
    [170, 201, 136, 200, 0.80, 0.0],          # duplicate box of card 1
    [468, 200, 200, 136, 0.90, math.pi / 2],  # card 2 as a rotated landscape box
    [320, 500, 50, 70, 0.10, 0.0],            # below detection threshold
]


class PipelineTests(unittest.TestCase):
    def assertPointNear(self, point, expected, tolerance=0.5):
        self.assertLessEqual(max(abs(point[0] - expected[0]), abs(point[1] - expected[1])), tolerance, point)

    def test_counts_orders_and_scores(self):
        results = fake_recognizer(TWO_CARDS).recognize(card_image())
        self.assertEqual([r["index"] for r in results], [1, 2])
        first, second = results
        top = first["candidates"][0]
        self.assertEqual((top["card_id"], top["name_en"], top["name_ja"]), ("11111111", "Red Top", "赤"))
        no_japanese = next(c for c in first["candidates"] if c["card_id"] == "22222222")
        self.assertIsNone(no_japanese["name_ja"])
        self.assertEqual(first["status"], "recognized")
        self.assertEqual(len(first["candidates"]), 3)
        self.assertAlmostEqual(sum(c["score"] for c in first["candidates"]), 1.0)
        self.assertPointNear(first["polygon"][0], [100, 100])
        self.assertPointNear(second["polygon"][0], [400, 100])
        self.assertIsInstance(first["crop"], Image.Image)
        self.assertAlmostEqual(first["detection_score"], 0.95, places=5)

    def test_flips_upside_down_card(self):
        second = fake_recognizer(TWO_CARDS).recognize(card_image(upside_down_second=True))[1]
        self.assertEqual(second["candidates"][0]["name_en"], "Red Top")
        # The card's top-left corner is now the box's bottom-right in the image.
        self.assertPointNear(second["polygon"][0], [536, 300])
        crop = np.asarray(second["crop"])
        self.assertEqual(crop[5, crop.shape[1] // 2].tolist(), [255, 0, 0])

    def test_low_confidence_is_marked_for_review(self):
        results = fake_recognizer(TWO_CARDS).recognize(card_image(), recognition_threshold=0.9999)
        self.assertEqual({r["status"] for r in results}, {"review"})

    def test_zero_detections_returns_empty_list(self):
        self.assertEqual(fake_recognizer([[320, 320, 50, 70, 0.1, 0.0]]).recognize(card_image()), [])

    def test_large_input_polygons_in_original_coordinates(self):
        # Working copy is 320 px; card 1 there is centred at (84, 100), size
        # 68x100, which the 640 letterbox doubles.
        with mock.patch.object(recognition, "MAX_WORKING_SIDE", 320):
            results = fake_recognizer([[168, 200, 136, 200, 0.95, 0.0]]).recognize(card_image())
        self.assertPointNear(results[0]["polygon"][0], [100, 100], tolerance=1.0)

    def test_detection_limit_raises(self):
        with mock.patch.object(recognition, "MAX_DETECTIONS", 1):
            with self.assertRaises(recognition.RecognitionLimitError):
                fake_recognizer(TWO_CARDS).recognize(card_image())

    def test_invalid_thresholds_rejected(self):
        recognizer = fake_recognizer(TWO_CARDS)
        for kwargs in ({"detection_threshold": float("nan")}, {"recognition_threshold": 1.5},
                       {"detection_threshold": -0.1}):
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                recognizer.recognize(card_image(), **kwargs)

    def test_card_found_only_by_rotated_pass_maps_back_upright(self):
        # 640x400 image: the rotated letterboxes differ from the upright one.
        image = Image.fromarray(np.asarray(card_image())[:400])
        for quarter_turns in (1, 2, 3):
            with self.subTest(quarter_turns=quarter_turns):
                rotated_rows = [[], [], []]
                rotated_rows[quarter_turns - 1] = [rotated_pass_row(168, 200, 136, 200, 0.9, quarter_turns, 640, 400)]
                results = fake_recognizer([], rotated_rows=rotated_rows).recognize(image)
                self.assertEqual(len(results), 1)
                card = results[0]
                self.assertPointNear(card["polygon"][0], [100, 100], tolerance=1.0)
                self.assertPointNear(card["polygon"][2], [236, 300], tolerance=1.0)
                self.assertEqual(card["candidates"][0]["name_en"], "Red Top")
                crop = np.asarray(card["crop"])
                self.assertEqual(crop[5, crop.shape[1] // 2].tolist(), [255, 0, 0])
                self.assertAlmostEqual(card["detection_score"], 0.9, places=5)

    def test_rotated_passes_never_replace_or_duplicate_upright_boxes(self):
        upright = [[168, 200, 136, 200, 0.6, 0.0]]
        rotated_rows = [
            [rotated_pass_row(170, 201, 136, 200, 0.99, 1, 640, 640),   # same card, higher score
             rotated_pass_row(168, 150, 100, 90, 0.95, 1, 640, 640)],   # partial box inside it
            [rotated_pass_row(168, 200, 136, 200, 0.97, 2, 640, 640)],
            [rotated_pass_row(168, 260, 120, 80, 0.98, 3, 640, 640)],   # contained lower part
        ]
        results = fake_recognizer(upright, rotated_rows=rotated_rows).recognize(card_image())
        self.assertEqual(len(results), 1)
        self.assertAlmostEqual(results[0]["detection_score"], 0.6, places=5)
        self.assertPointNear(results[0]["polygon"][0], [100, 100])
        self.assertPointNear(results[0]["polygon"][2], [236, 300])

    def test_rotated_pass_adds_touching_identical_neighbour(self):
        # Two identical cards side by side; only the rotated pass sees the right one.
        image = np.full((640, 640, 3), 40, dtype=np.uint8)
        for left in (100, 236):
            image[100:200, left:left + 136] = (255, 0, 0)
            image[200:300, left:left + 136] = (0, 0, 255)
        rotated_rows = [[rotated_pass_row(168, 200, 136, 200, 0.9, 1, 640, 640),
                         rotated_pass_row(304, 200, 136, 200, 0.9, 1, 640, 640)], [], []]
        results = fake_recognizer([[168, 200, 136, 200, 0.95, 0.0]], rotated_rows=rotated_rows).recognize(
            Image.fromarray(image))
        self.assertEqual(len(results), 2)
        self.assertPointNear(results[0]["polygon"][0], [100, 100])
        self.assertPointNear(results[1]["polygon"][0], [236, 100], tolerance=1.0)
        self.assertEqual([r["candidates"][0]["name_en"] for r in results], ["Red Top", "Red Top"])

    def test_detection_limit_counts_rotated_pass_boxes(self):
        rotated_rows = [[], [rotated_pass_row(468, 200, 136, 200, 0.9, 2, 640, 640)], []]
        with mock.patch.object(recognition, "MAX_DETECTIONS", 1):
            with self.assertRaises(recognition.RecognitionLimitError):
                fake_recognizer([[168, 200, 136, 200, 0.95, 0.0]], rotated_rows=rotated_rows).recognize(card_image())

    def test_corrupt_rotated_pass_output_raises(self):
        valid = [[168, 200, 136, 200, 0.95, 0.0]]
        with self.assertRaisesRegex(RuntimeError, "non-finite"):
            fake_recognizer(valid, rotated_rows=[[], [], [[100, 100, 50, 70, float("nan"), 0.0]]]).recognize(
                card_image())
        with self.assertRaisesRegex(RuntimeError, "non-positive"):
            fake_recognizer(valid, rotated_rows=[[[100, 100, 50, -1, 0.9, 0.0]], [], []]).recognize(card_image())

    def test_corrupt_model_outputs_raise(self):
        with self.assertRaisesRegex(RuntimeError, "non-finite"):
            fake_recognizer([[168, 200, 136, 200, float("nan"), 0.0]]).recognize(card_image())
        with self.assertRaisesRegex(RuntimeError, "non-positive"):
            fake_recognizer([[168, 200, -5, 200, 0.9, 0.0]]).recognize(card_image())

        class WrongWidthClassifier:
            def run(self, _outputs, inputs):
                return [np.zeros((len(next(iter(inputs.values()))), 2), dtype=np.float32)]
        with self.assertRaisesRegex(RuntimeError, "Classifier returned shape"):
            fake_recognizer(TWO_CARDS, WrongWidthClassifier()).recognize(card_image())


# ---------------------------------------------------------------- model files

class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ModelFileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def patch_models(self, files, payload):
        calls = []
        self.enterContext(mock.patch.object(recognition, "MODEL_FILES", files))
        self.enterContext(mock.patch.object(
            recognition.urllib.request, "urlopen",
            lambda url, timeout: calls.append(url) or FakeResponse(payload)))
        return calls

    def test_recognizer_without_models_fails_clearly(self):
        self.assertFalse(models_ready(self.directory))
        with self.assertRaisesRegex(FileNotFoundError, "download_models"):
            Recognizer(self.directory)

    def test_recognizer_rejects_same_size_modified_file(self):
        good, bad = b'{"0": {}}', b'{"1": {}}'
        self.patch_models({"cardnames_onnx.json": (len(good), hashlib.sha256(good).hexdigest())}, good)
        (self.directory / "cardnames_onnx.json").write_bytes(bad)
        self.assertTrue(models_ready(self.directory))
        with self.assertRaisesRegex(RuntimeError, "cardnames_onnx.json has sha256"):
            Recognizer(self.directory)

    def test_download_rejects_hash_mismatch_atomically(self):
        payload = b"not the model"
        self.patch_models({"m.onnx": (len(payload), "0" * 64)}, payload)
        with self.assertRaisesRegex(RuntimeError, "m.onnx.*sha256 mismatch"):
            download_models(self.directory)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_download_rejects_truncated_file(self):
        payload = b"short"
        self.patch_models({"m.onnx": (100, hashlib.sha256(payload).hexdigest())}, payload)
        with self.assertRaisesRegex(RuntimeError, "size mismatch"):
            download_models(self.directory)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_download_writes_verified_file_and_skips_it_next_time(self):
        payload = b"model bytes"
        calls = self.patch_models({"m.onnx": (len(payload), hashlib.sha256(payload).hexdigest())}, payload)
        messages = []
        download_models(self.directory, progress=messages.append)
        self.assertEqual((self.directory / "m.onnx").read_bytes(), payload)
        self.assertTrue(models_ready(self.directory))
        download_models(self.directory, progress=messages.append)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].endswith(f"/resolve/{recognition.HF_REVISION}/onnx/m.onnx"))
        self.assertIn("m.onnx already present and verified", messages)


if __name__ == "__main__":
    unittest.main()
