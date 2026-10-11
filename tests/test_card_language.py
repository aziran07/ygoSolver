"""Independent contract for physical-edition evidence and operational failures."""
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import numpy as np
from PIL import Image

import card_language as language


def characters(text, probability=0.95):
    return [{"text": char, "probability": probability} for char in text]


class EditionEvidenceTest(unittest.TestCase):
    def test_hangul_identifies_korean_even_with_latin_card_name(self):
        for text in ("마법 카드 이 카드를 발동한다", "Evil★Twin 키스킬 몬스터 효과"):
            result = language.classify_characters(characters(text), characters("Evil Twin"))
            self.assertEqual((result["locale"], result["status"]), ("ko", "classified"))

    def test_kana_identifies_japanese_including_halfwidth(self):
        for text in ("このカードが召喚された場合", "モンスターの効果を発動", "ﾓﾝｽﾀｰ ｶｰﾄﾞ"):
            with self.subTest(text=text):
                result = language.classify_characters([], characters(text))
                self.assertEqual((result["locale"], result["status"]), ("ja", "classified"))

    def test_absence_of_hangul_never_becomes_japanese(self):
        for text in ("", "ATK 2500 DEF 2100", "Dark Magician", "青眼白龍 魔法 効果", "①★／！？", "ーー・・ｰｰ"):
            with self.subTest(text=text):
                result = language.classify_characters(characters(text), characters(text))
                self.assertIsNone(result["locale"])
                self.assertEqual(result["status"], "review")
                self.assertTrue(result["reason"])

    def test_weak_or_conflicting_evidence_requires_review(self):
        samples = [(characters("마법 카드 효과", 0.1), []), ([], characters("このカード", 0.1)),
                   (characters("마법 카드 효과 발동"), characters("このカードの効果")),
                   (characters("가"), []), ([], characters("カ")),
                   (characters("마법 카드 효과 발동"), characters("の"))]
        for korean, japanese in samples:
            with self.subTest(korean=korean, japanese=japanese):
                result = language.classify_characters(korean, japanese)
                self.assertIsNone(result["locale"])
                self.assertEqual(result["status"], "review")

    def test_malformed_evidence_is_an_error_not_unknown_language(self):
        invalid = [None, [{}], [{"text": None, "probability": .95}], characters("카드", float("nan")),
                   characters("カード", float("inf")), characters("카드", 1.01), characters("가", True),
                   [{"text": "카드", "probability": .9}]]
        for sample in invalid:
            with self.subTest(sample=sample), self.assertRaises((ValueError, language.LanguageDetectionError)):
                language.classify_characters(sample, [])


class OriginalImageTest(unittest.TestCase):
    def test_crop_preserves_source_detail_and_orientation(self):
        rgb = np.full((1400, 1000, 3), 128, dtype=np.uint8)
        rgb[100:650, 100:850] = (240, 0, 0)
        rgb[650:1200, 100:850] = (0, 0, 240)
        polygon = np.array([[100, 100], [850, 100], [850, 1200], [100, 1200]], dtype=float)
        crop = language.extract_card(Image.fromarray(rgb), polygon)
        self.assertGreaterEqual(crop.height, 1000, "OCR must not reuse the 400px recognition crop")
        self.assertEqual(crop.getpixel((crop.width // 2, 30)), (240, 0, 0))
        self.assertEqual(crop.getpixel((crop.width // 2, crop.height - 30)), (0, 0, 240))
        flipped = language.extract_card(Image.fromarray(rgb), polygon[[2, 3, 0, 1]])
        self.assertEqual(flipped.getpixel((flipped.width // 2, 30)), (0, 0, 240))

    def test_bad_geometry_is_rejected_before_ocr(self):
        bad_polygons = [[], [[0, 0]] * 4, [[0, 0], [20, 20], [20, 0], [0, 20]],
                        [[0, 0], [float("nan"), 0], [20, 20], [0, 20]]]
        for polygon in bad_polygons:
            with self.subTest(polygon=polygon), self.assertRaises((ValueError, language.LanguageDetectionError)):
                language.extract_card(Image.new("RGB", (100, 100)), polygon)


class OCRBoundaryTest(unittest.TestCase):
    def session(self, output):
        session = mock.Mock()
        session.get_inputs.return_value = [SimpleNamespace(name="input")]
        session.run.return_value = [output]
        return session

    def test_ctc_blank_separates_repeated_letters_and_repeated_frames_collapse(self):
        # Independent CTC example: repeated frames for one character collapse;
        # the same character after a blank is a second printed character.
        sequence = [0, 1, 1, 0, 1, 2, 2]
        output = np.eye(3, dtype=np.float32)[sequence][None]
        session = self.session(output)
        reading = language.recognize_line((session, [None, "가", "나"]), np.zeros((24, 100, 3), dtype=np.uint8))
        self.assertEqual("".join(c["text"] for c in reading), "가가나")
        self.assertTrue(all(c["probability"] == 1 for c in reading))

    def test_valid_blank_reading_and_empty_text_map_have_no_evidence(self):
        session = self.session(np.array([[[1., 0., 0.]]], dtype=np.float32))
        self.assertEqual(language.recognize_line((session, [None, "가", "나"]),
                                                np.zeros((24, 100, 3), dtype=np.uint8)), [])
        detector = self.session(np.zeros((1, 1, 736, 736), dtype=np.float32))
        self.assertEqual(language.detect_text_lines(detector, np.zeros((736, 736, 3), dtype=np.uint8)), [])

    def test_model_inputs_follow_upstream_bgr_channel_order(self):
        # RapidOCR loads PIL RGB as BGR, then normalizes each channel by .5/.5.
        detector = self.session(None)
        def text_in_title(_names, inputs):
            height, width = inputs["input"].shape[2:]
            output = np.zeros((1, 1, height, width), dtype=np.float32)
            output[0, 0, height // 20:height // 10, width // 10:width * 8 // 10] = 1
            return [output]
        detector.run.side_effect = text_in_title
        reader = self.session(np.array([[[1., 0.]]], dtype=np.float32))
        models = {"detector": detector, "ko": (reader, [None, "가"]), "ja": (reader, [None, "カ"])}
        with mock.patch.object(language, "load_models", return_value=models):
            language.detect_language(Image.new("RGB", (400, 600), "red"),
                                     [[0, 0], [400, 0], [400, 600], [0, 600]])
        for session in (detector, reader):
            self.assertTrue(session.run.called)
            tensor = session.run.call_args.args[1]["input"]
            np.testing.assert_array_equal(tensor[0, :, 0, 0], [-1., -1., 1.])

    def test_invalid_recognizer_output_is_explicit_failure(self):
        outputs = [np.zeros((1, 3)), np.zeros((1, 2, 4)), np.zeros((1, 0, 3)),
                   np.full((1, 2, 3), np.nan), np.full((1, 2, 3), np.inf),
                   np.full((1, 2, 3), -0.1), np.full((1, 2, 3), 1.1), np.zeros((1, 2, 3))]
        for output in outputs:
            with self.subTest(shape=output.shape, sample=output.flatten()[:1]), \
                    self.assertRaises(language.LanguageDetectionError):
                language.recognize_line((self.session(output), [None, "가", "나"]),
                                        np.zeros((24, 100, 3), dtype=np.uint8))

    def test_invalid_detector_output_cannot_masquerade_as_no_text(self):
        outputs = [np.zeros((1, 1, 32, 32)), np.full((1, 1, 736, 736), np.nan),
                   np.full((1, 1, 736, 736), -1.), np.full((1, 1, 736, 736), 1.1)]
        for output in outputs:
            with self.subTest(shape=output.shape, sample=output.flatten()[:1]), \
                    self.assertRaises(language.LanguageDetectionError):
                language.detect_text_lines(self.session(output), np.zeros((736, 736, 3), dtype=np.uint8))

    def test_runtime_failure_names_the_failed_ocr_stage(self):
        session = self.session(None)
        session.run.side_effect = RuntimeError("inference device failed")
        image = np.zeros((32, 100, 3), dtype=np.uint8)
        for operation in (lambda: language.recognize_line((session, [None, "가"]), image),
                          lambda: language.detect_text_lines(session, image)):
            with self.assertRaisesRegex(language.LanguageDetectionError, "inference device failed"):
                operation()

    def test_no_runtime_outputs_are_not_an_empty_reading(self):
        session = self.session(None)
        session.run.return_value = []
        image = np.zeros((32, 100, 3), dtype=np.uint8)
        for operation in (lambda: language.recognize_line((session, [None, "가"]), image),
                          lambda: language.detect_text_lines(session, image)):
            with self.assertRaises(language.LanguageDetectionError):
                operation()

    def test_missing_and_corrupt_model_fail_before_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            language.load_models.cache_clear()
            with self.assertRaisesRegex(language.LanguageDetectionError, "missing"):
                language.load_models(Path(directory))
            model = Path(directory) / "example.onnx"
            model.write_bytes(b"bad")
            expected = hashlib.sha256(b"yes").hexdigest()
            with mock.patch.object(language, "OCR_MODELS", {"detector": (model.name, "det", 3, expected)}), \
                    mock.patch.object(language.ort, "InferenceSession") as infer:
                with self.assertRaisesRegex(language.LanguageDetectionError, "sha256"):
                    language.load_models(Path(directory))
                infer.assert_not_called()
        language.load_models.cache_clear()

    def test_model_load_exception_is_preserved_and_not_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "example.onnx"
            model.write_bytes(b"yes")
            expected = hashlib.sha256(b"yes").hexdigest()
            language.load_models.cache_clear()
            with mock.patch.object(language, "OCR_MODELS", {"detector": (model.name, "det", 3, expected)}), \
                    mock.patch.object(language.ort, "InferenceSession", side_effect=RuntimeError("invalid ONNX")) as infer:
                for _ in range(2):
                    with self.assertRaisesRegex(language.LanguageDetectionError, "invalid ONNX"):
                        language.load_models(Path(directory))
                self.assertEqual(infer.call_count, 2)
        language.load_models.cache_clear()


if __name__ == "__main__":
    unittest.main()
