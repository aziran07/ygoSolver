"""Codex UI acceptance for optional top-three official artwork comparison."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import streamlit as st
from PIL import Image
from streamlit.testing.v1 import AppTest

import catalog
import recognition
import references
import rarities
import deck_order
from export_order_fixtures import app_sort_keys
from test_app import APP_PATH, ASH, JA_ONLY, LIBRARY, download_buttons_disabled, png_bytes, reference_match


class CandidateModeTest(unittest.TestCase):
    def setUp(self):
        sort_patch = mock.patch.object(deck_order, "get_sort_keys", side_effect=app_sort_keys)
        sort_patch.start()
        self.addCleanup(sort_patch.stop)
        rarity_patch = mock.patch.object(rarities, "get_rarities", return_value=["N", "SR"])
        rarity_patch.start()
        self.addCleanup(rarity_patch.stop)
        st.cache_resource.clear()
        st.cache_data.clear()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        image_path = Path(directory.name) / "alternate.png"
        image_path.write_bytes(png_bytes("green"))
        self.cards = [ASH, {**JA_ONLY, "image_path": str(image_path)}]
        self.candidates = [{"card_id": "111", "name_en": ASH["name_en"], "name_ja": ASH["name_ja"], "score": .0149},
                           {"card_id": "222", "name_en": JA_ONLY["name_en"], "name_ja": JA_ONLY["name_ja"], "score": .0126}]
        self.detection = {"index": 1, "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]],
                          "crop": Image.new("RGB", (64, 48)), "candidates": self.candidates,
                          "status": "review", "detection_score": .9}
        self.recognizer = mock.Mock()
        self.recognizer.recognize.return_value = [self.detection]
        self.reviewer = mock.Mock()
        self.reviewer.review.return_value = self.review_result(accepted=False)
        self.reviewer_factory = mock.Mock(return_value=self.reviewer)
        self.matcher = mock.Mock()
        self.matcher.match.return_value = None
        self.library = mock.Mock(return_value=None)
        self.resolve = mock.Mock(side_effect=lambda name: next(c for c in self.cards if c["name_en"] == name))
        patches = [mock.patch.object(recognition, "models_ready", return_value=True),
                   mock.patch.object(recognition, "Recognizer", return_value=self.recognizer),
                   mock.patch.object(references, "library_info", self.library),
                   mock.patch.object(references, "ReferenceMatcher", return_value=self.matcher),
                   mock.patch.object(references, "CandidateReviewer", self.reviewer_factory),
                   mock.patch.object(catalog, "resolve_card", self.resolve)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def review_result(self, accepted):
        reviewed = []
        for i, (candidate, card) in enumerate(zip(self.candidates, self.cards)):
            reviewed.append({**candidate, "review": {"cid": card["cid"], "ciid": 2 if i else 1, "card": card,
                            "inliers": (22 if accepted else 8) if i else 4, "inlier_ratio": .4, "spread": .25,
                            "geometry_problem": None, "margin": 2.0 if i else None,
                            "outcome": ("verified" if accepted else "leading") if i else "none"}})
        return {"candidates": [reviewed[1], reviewed[0]], "accepted": accepted}

    def new_app(self):
        app = AppTest.from_file(APP_PATH, default_timeout=30).run()
        app.file_uploader[0].set_value([("deck.png", png_bytes("red"), "image/png")]).run()
        return app

    def recognize(self, app):
        next(button for button in app.button if button.label == "카드 인식").click().run()
        self.assertFalse(app.exception)
        return app

    def test_default_on_without_base_library_keeps_weak_winner_for_review(self):
        app = self.recognize(self.new_app())
        self.assertTrue(app.toggle(key="use_candidate_review").value)
        self.assertFalse(app.toggle(key="use_references").value)
        self.reviewer.review.assert_called_once_with(self.detection["crop"], self.candidates)
        selected = app.session_state["rows"][0]
        self.assertEqual((selected["choice"], selected["card"]["cid"], selected["status"]), ("222", 5050, "review"))
        self.assertEqual(selected["candidates"][0]["score"], .0126)
        label = app.selectbox(key=f"choice_{selected['key']}").options[1]
        for text in ("1.26%", "8", "확인 필요"):
            self.assertIn(text, label)
        self.assertTrue(any("확인 필요 1장" in warning.value for warning in app.warning))
        self.assertEqual(download_buttons_disabled(app), [True, True])
        self.resolve.assert_not_called()  # reviewed official identity is used directly

    def test_strong_winner_is_recognized_but_export_still_needs_confirmation(self):
        self.reviewer.review.return_value = self.review_result(accepted=True)
        app = self.recognize(self.new_app())
        self.assertEqual(app.session_state["rows"][0]["status"], "recognized")
        self.assertTrue(any("후보 일러스트 검증 1장" in success.value for success in app.success))
        app.radio[0].set_value("일본어").run()
        self.assertEqual(download_buttons_disabled(app), [True, True])
        app.checkbox(key="confirmed").check().run()
        self.assertEqual(download_buttons_disabled(app), [False, False])

    def test_manual_choice_quantity_and_name_survive_reruns_without_recomparison(self):
        app = self.recognize(self.new_app())
        key = app.session_state["rows"][0]["key"]
        app.selectbox(key=f"choice_{key}").set_value(2).run()
        app.number_input(key=f"quantity_{key}").set_value(3).run()
        app.text_input(key=f"name_{key}").set_value("사용자 카드명").run()
        app.radio[0].set_value("일본어").run()
        app.run()
        selected = app.session_state["rows"][0]
        self.assertEqual((selected["choice"], selected["quantity"], selected["name_override"]),
                         ("111", 3, "사용자 카드명"))
        self.assertEqual(self.reviewer.review.call_count, 1)

    def test_old_signature_migrates_without_discarding_edits_or_confirmation(self):
        app = self.new_app()
        app.toggle(key="use_candidate_review").set_value(False).run()
        self.recognize(app)
        key = app.session_state["rows"][0]["key"]
        app.number_input(key=f"quantity_{key}").set_value(3).run()
        app.checkbox(key="confirmed").check().run()
        app.session_state["recognized_signature"] = app.session_state["recognized_signature"][:3]
        app.run()
        self.assertEqual(app.session_state["rows"][0]["quantity"], 3)
        self.assertTrue(app.checkbox(key="confirmed").value)
        self.assertEqual(download_buttons_disabled(app), [False, False])

    def test_strong_global_match_does_not_prepare_or_call_candidate_review(self):
        self.library.return_value = LIBRARY
        self.matcher.match.return_value = reference_match(ASH)
        app = self.recognize(self.new_app())
        self.assertEqual(app.session_state["rows"][0]["status"], "recognized")
        self.reviewer_factory.assert_not_called()

    def test_high_confidence_skips_review_but_small_gap_triggers_it(self):
        self.detection["status"] = "recognized"
        self.detection["candidates"] = [{**self.candidates[0], "score": .9}, {**self.candidates[1], "score": .05}]
        app = self.recognize(self.new_app())
        self.reviewer_factory.assert_not_called()
        self.detection["candidates"] = [{**self.candidates[0], "score": .52}, {**self.candidates[1], "score": .48}]
        self.recognize(app)
        self.assertEqual(self.reviewer.review.call_count, 1)
        self.assertEqual(app.session_state["rows"][0]["status"], "review")

    def test_network_failure_aborts_instead_of_returning_partial_success(self):
        app = self.recognize(self.new_app())
        self.reviewer.review.side_effect = RuntimeError("CID 5050 artwork HTTP 503")
        self.recognize(app)
        self.assertTrue(any("CID 5050 artwork HTTP 503" in error.value for error in app.error))
        self.assertEqual(app.session_state["rows"], [])
        self.assertIsNone(app.session_state["recognized_signature"])
        self.assertEqual(download_buttons_disabled(app), [True, True])

    def test_turning_off_review_invalidates_results_and_uses_model_on_next_run(self):
        app = self.recognize(self.new_app())
        app.toggle(key="use_candidate_review").set_value(False).run()
        self.assertEqual(app.session_state["rows"], [])
        self.assertEqual(download_buttons_disabled(app), [True, True])
        self.recognize(app)
        self.assertEqual(app.session_state["rows"][0]["choice"], "111")
        self.assertEqual(self.reviewer.review.call_count, 1)


if __name__ == "__main__":
    unittest.main()
