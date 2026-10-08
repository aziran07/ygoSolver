import io
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

APP_PATH = str(Path(__file__).resolve().parents[1] / "app.py")

ASH = {"cid": 12950, "name_en": "Ash Blossom & Joyous Spring", "name_ko": "하루 우라라", "name_ja": "灰流うらら",
       "image_path": None, "source_url": "https://www.db.yugioh-card.com/"}
CANDIDATES = [{"card_id": "14558127", "name_en": ASH["name_en"], "name_ja": ASH["name_ja"], "score": 0.42}]
JA_ONLY = {"cid": 5050, "name_en": "Maxx \"C\"", "name_ko": None, "name_ja": "増殖するG",
           "image_path": None, "source_url": "https://www.db.yugioh-card.com/"}
NO_LOCAL_NAME = {"cid": 7070, "name_en": "Overseas Only", "name_ko": None, "name_ja": None,
                 "image_path": None, "source_url": "https://www.db.yugioh-card.com/"}
LOCALIZED_CANDIDATES = CANDIDATES + [
    {"card_id": "23434538", "name_en": JA_ONLY["name_en"], "name_ja": "model JA name", "score": 0.30},
    {"card_id": "99999999", "name_en": NO_LOCAL_NAME["name_en"], "name_ja": "model JA name", "score": 0.10}]


def row(key, card=None, candidates=(), status="recognized", quantity=1):
    return {"key": key, "source": "manual", "image_name": None, "index": None, "crop": None,
            "candidates": list(candidates), "status": status, "choice": None, "card": card, "error": None,
            "quantity": quantity, "name_override": ""}


def start_app(rows):
    app = AppTest.from_file(APP_PATH, default_timeout=30)
    app.session_state["rows"] = rows
    return app


def download_buttons_disabled(app):
    return [button.proto.disabled for button in app.get("download_button")]


class AppTestCase(unittest.TestCase):
    def setUp(self):
        sort_patch = mock.patch.object(deck_order, "get_sort_keys", side_effect=app_sort_keys)
        sort_patch.start()
        self.addCleanup(sort_patch.stop)
        rarity_patch = mock.patch.object(rarities, "get_rarities", return_value=["N", "SR"])
        rarity_patch.start()
        self.addCleanup(rarity_patch.stop)
        # No reference library unless a test says otherwise (keeps tests independent of data/references).
        patch = mock.patch.object(references, "library_info", return_value=None)
        patch.start()
        self.addCleanup(patch.stop)

    def test_missing_models_offers_download_and_nothing_else(self):
        with mock.patch.object(recognition, "models_ready", return_value=False):
            app = AppTest.from_file(APP_PATH, default_timeout=30).run()
        self.assertFalse(app.exception)
        self.assertIn("427MB", app.warning[0].value)
        self.assertEqual(app.button[0].label, "모델 다운로드 (약 427MB)")
        self.assertEqual(len(app.get("file_uploader")), 0)

    def test_model_download_failure_is_shown(self):
        failure = RuntimeError("Downloading ygo_yolo.onnx from https://huggingface.co/x failed: HTTP 503")
        with mock.patch.object(recognition, "models_ready", return_value=False), \
                mock.patch.object(recognition, "download_models", side_effect=failure):
            app = AppTest.from_file(APP_PATH, default_timeout=30).run()
            app.button[0].click().run()
        self.assertIn("HTTP 503", app.error[0].value)

    def test_low_confidence_row_blocks_export_and_lookup_error_is_shown(self):
        with mock.patch.object(recognition, "models_ready", return_value=True), \
                mock.patch.object(catalog, "resolve_card", side_effect=catalog.CatalogError("offline")):
            app = start_app([row("a", candidates=CANDIDATES, status="review")]).run()
            self.assertEqual(download_buttons_disabled(app), [True, True])
            self.assertTrue(any("후보를 선택" in w.value for w in app.warning))
            app.selectbox(key="choice_a").set_value(1).run()
        self.assertFalse(app.exception)
        self.assertTrue(any("offline" in w.value for w in app.warning))
        self.assertEqual(download_buttons_disabled(app), [True, True])

    def test_candidate_labels_use_korean_then_japanese_regardless_of_export_language(self):
        def resolve(name_en):
            return {card["name_en"]: card for card in (ASH, JA_ONLY, NO_LOCAL_NAME)}[name_en]

        with mock.patch.object(recognition, "models_ready", return_value=True), \
                mock.patch.object(catalog, "resolve_card", side_effect=resolve):
            app = start_app([row("a", candidates=LOCALIZED_CANDIDATES, status="review")]).run()
            expected = ["— 선택하세요 —", "하루 우라라 (42%)", "増殖するG (30%)",
                        "한국어·일본어 공식 이름 없음 (DRAW2 ID 99999999) (10%)"]
            self.assertEqual(app.selectbox(key="choice_a").options, expected)
            for export_language in ("일본어", "영어", "한국어"):
                app.radio[0].set_value(export_language).run()
                self.assertEqual(app.selectbox(key="choice_a").options, expected)

            app.radio[0].set_value("일본어").run()
            app.selectbox(key="choice_a").set_value(2).run()
            selected = app.session_state["rows"][0]
            self.assertEqual((selected["choice"], selected["card"]["cid"]), ("23434538", 5050))
            app.checkbox(key="export_cid").check().run()
            table = app.dataframe[0].value
            self.assertEqual((list(table["카드명"]), list(table["공식 CID"])), (["増殖するG"], [5050]))
            app.checkbox(key="confirmed").check().run()
        self.assertFalse(app.exception)
        self.assertEqual(download_buttons_disabled(app), [False, False])

    def test_lookup_error_label_is_explicit_cached_and_retried_only_on_request(self):
        resolve = mock.Mock(side_effect=catalog.CatalogError("offline"))
        alias = {**CANDIDATES[0], "card_id": "14558128", "name_en": "ash blossom  & JOYOUS spring", "score": 0.2}
        with mock.patch.object(recognition, "models_ready", return_value=True),                 mock.patch.object(catalog, "resolve_card", resolve):
            app = start_app([row("a", candidates=CANDIDATES, status="review"),
                             row("b", candidates=CANDIDATES, status="review"),
                             row("c", candidates=CANDIDATES + [alias], status="review")]).run()
            self.assertEqual(app.selectbox(key="choice_a").options[1], "공식 이름 조회 실패 (DRAW2 ID 14558127) (42%)")
            self.assertNotIn(ASH["name_en"], " ".join(app.selectbox(key="choice_a").options))
            app.run()
            app.selectbox(key="choice_a").set_value(1).run()
            app.selectbox(key="choice_b").set_value(1).run()
            # three rows, an equivalent alias, reruns and both selections reuse one failed lookup
            self.assertEqual(resolve.call_count, 1)
            for selected in app.session_state["rows"][:2]:
                self.assertEqual((selected["choice"], selected["card"]), ("14558127", None))
                self.assertIn("offline", selected["error"])
            self.assertEqual(download_buttons_disabled(app), [True, True])

            resolve.side_effect = None
            resolve.return_value = ASH
            app.button(key="retry_c").click().run()  # one retry, from a row that selected nothing
            self.assertEqual(resolve.call_count, 2)
            self.assertFalse(app.exception)
            self.assertEqual(app.selectbox(key="choice_a").options[1], "하루 우라라 (42%)")
            for selected in app.session_state["rows"][:2]:
                self.assertEqual((selected["choice"], selected["card"]["cid"], selected["error"]),
                                 ("14558127", ASH["cid"], None))
            unselected = app.session_state["rows"][2]
            self.assertEqual((unselected["choice"], unselected["card"], unselected["error"]), (None, None, None))
            self.assertFalse(any("offline" in w.value for w in app.warning))
            self.assertFalse([button for button in app.button if (button.key or "").startswith("retry_")])
            self.assertEqual(download_buttons_disabled(app), [True, True])  # row c still needs a choice

            app.button(key="delete_c").click().run()
            self.assertEqual(download_buttons_disabled(app), [True, True])  # final confirmation still required
            app.checkbox(key="export_cid").check().run()
            self.assertEqual(list(app.dataframe[0].value["공식 CID"]), [ASH["cid"]])
            app.checkbox(key="confirmed").check().run()
        self.assertEqual(download_buttons_disabled(app), [False, False])
        self.assertEqual(resolve.call_count, 2)

    def test_duplicates_summed_and_export_needs_final_confirmation(self):
        with mock.patch.object(recognition, "models_ready", return_value=True):
            app = start_app([row("a", card=ASH), row("b", card=ASH, quantity=2)]).run()
            self.assertEqual(download_buttons_disabled(app), [True, True])
            table = app.dataframe[0].value
            self.assertEqual(list(table["수량"]), [3])
            self.assertEqual(list(table["카드명"]), ["하루 우라라"])
            app.checkbox(key="confirmed").check().run()
        self.assertEqual(download_buttons_disabled(app), [False, False])

    def test_retry_resolves_apostrophe_name_through_catalog_and_preserves_edits(self):
        # Exercise the real parser/resolver, starting with the error left in a live session.
        from test_catalog import FakeSession, detail_html, search_html

        name = "Evil★Twin's Trouble Sunny"
        korean = "Evil★Twin's 트러블 써니"
        candidate = {"card_id": "93672138", "name_en": name, "score": 0.46}
        failure = f'no official card named exactly "{name}" (search returned 1 results)'
        selected = row("sunny", candidates=[candidate], status="review", quantity=3)
        selected.update(choice="93672138", error=failure, name_override="내 카드")
        duplicate = row("duplicate", candidates=[candidate], status="review", quantity=2)
        duplicate.update(choice="93672138", error=failure)
        unselected = row("unselected", candidates=[candidate], status="review")
        session = FakeSession({1: search_html([(name, 16537)])}, {
            "en": detail_html(name, cid=16537),
            "ko": detail_html(korean, cid=16537),
            "ja": detail_html("Evil★Twin’s トラブル・サニー", cid=16537),
        }, image=png_bytes("green"))
        resolve_card = catalog.resolve_card
        with tempfile.TemporaryDirectory() as cache, \
                mock.patch.object(recognition, "models_ready", return_value=True), \
                mock.patch.object(catalog, "new_session", return_value=session) as new_session, \
                mock.patch.object(catalog, "resolve_card", side_effect=lambda name: resolve_card(name, Path(cache))):
            app = start_app([selected, duplicate, unselected])
            app.session_state["card_lookups"] = {catalog.normalize_name(name): {"error": failure}}
            app.run()
            app.run()
            new_session.assert_not_called()
            self.assertEqual(download_buttons_disabled(app), [True, True])
            old_widget_id = app.selectbox(key="choice_sunny").proto.id
            app.button(key="retry_sunny").click().run()
            self.assertFalse(app.exception)
            new_session.assert_called_once_with()
            self.assertEqual(sum(params is not None for _, params, _ in session.requests), 1)
            self.assertTrue(session.closed)
            first, second, third = app.session_state["rows"]
            for resolved in (first, second):
                self.assertEqual((resolved["choice"], resolved["card"]["cid"], resolved["error"]),
                                 ("93672138", 16537, None))
                self.assertEqual(resolved["card"]["name_ko"], korean)
                self.assertEqual(resolved["status"], "review")
            self.assertEqual((first["quantity"], first["name_override"], second["quantity"]), (3, "내 카드", 2))
            self.assertEqual((third["choice"], third["card"]), (None, None))
            self.assertIn(korean, app.selectbox(key="choice_sunny").options[1])
            refreshed = app.selectbox(key="choice_sunny").proto
            if refreshed.id == old_widget_id:
                # The browser keeps the old selected string unless the server sends a value update.
                self.assertTrue(refreshed.set_value, "Recovered label must be sent to the existing browser widget")
                self.assertEqual(refreshed.raw_value, f"{korean} (46%)")
            self.assertFalse(any(failure in warning.value for warning in app.warning))
            self.assertEqual(download_buttons_disabled(app), [True, True])

    def test_failed_retry_keeps_new_error_visible_and_blocks_export(self):
        candidate = CANDIDATES[0]
        selected = row("a", candidates=CANDIDATES, status="review", quantity=3)
        selected.update(choice=candidate["card_id"], error="old error", name_override="수정한 이름")
        with mock.patch.object(recognition, "models_ready", return_value=True), \
                mock.patch.object(catalog, "resolve_card", side_effect=catalog.CatalogError("HTTP 503")) as resolve:
            app = start_app([selected])
            app.session_state["card_lookups"] = {catalog.normalize_name(candidate["name_en"]): {"error": "old error"}}
            app.run()
            app.button(key="retry_a").click().run()
            self.assertFalse(app.exception)
            resolve.assert_called_once_with(candidate["name_en"])
            current = app.session_state["rows"][0]
            self.assertEqual((current["choice"], current["card"], current["quantity"], current["name_override"]),
                             (candidate["card_id"], None, 3, "수정한 이름"))
            self.assertIn("HTTP 503", current["error"])
            self.assertTrue(any("HTTP 503" in warning.value for warning in app.warning))
            self.assertEqual(download_buttons_disabled(app), [True, True])

    def test_retry_resolves_reactor_alias_from_existing_session_error(self):
        from test_catalog import FakeSession, detail_html, search_html

        candidate = {"card_id": "15175429", "name_en": "Spell Reactor RE", "score": 0.42}
        failure = "no official card named exactly 'Spell Reactor RE' (search returned 1 results)"
        selected = row("reactor", candidates=[candidate], status="review", quantity=3)
        selected.update(choice="15175429", error=failure, name_override="내 카드")
        official = "Spell Reactor ・RE"
        session = FakeSession({1: search_html([(official, 8002)])}, {
            "en": detail_html(official, cid=8002),
            "ko": detail_html("매직 리액터 AID", cid=8002),
            "ja": detail_html("マジック・リアクター・ＡＩＤ", cid=8002),
        }, image=png_bytes("green"))
        resolve_card = catalog.resolve_card
        with tempfile.TemporaryDirectory() as cache, \
                mock.patch.object(recognition, "models_ready", return_value=True), \
                mock.patch.object(catalog, "new_session", return_value=session) as new_session, \
                mock.patch.object(catalog, "resolve_card", side_effect=lambda name: resolve_card(name, Path(cache))):
            app = start_app([selected])
            # Literal key from the pre-fix app, so changing normalization cannot hide this old error.
            app.session_state["card_lookups"] = {"spell reactor re": {"error": failure}}
            app.run()
            new_session.assert_not_called()
            app.button(key="retry_reactor").click().run()
            self.assertFalse(app.exception)
            current = app.session_state["rows"][0]
            self.assertIsNone(current["error"])
            self.assertEqual((current["card"]["cid"], current["card"]["name_en"]), (8002, official))
            self.assertEqual((current["choice"], current["quantity"], current["name_override"], current["status"]),
                             ("15175429", 3, "내 카드", "review"))
            self.assertEqual(app.selectbox(key="choice_reactor").options[1], "매직 리액터 AID (42%)")
            self.assertFalse(any(failure in warning.value for warning in app.warning))
            self.assertEqual(download_buttons_disabled(app), [True, True])

    def test_missing_locale_name_needs_manual_name(self):
        card = {**ASH, "name_ko": None}
        with mock.patch.object(recognition, "models_ready", return_value=True):
            app = start_app([row("a", card=card)]).run()
            self.assertTrue(any("공식 이름이 없습니다" in w.value for w in app.warning))
            self.assertEqual(len(app.dataframe), 0)
            app.text_input(key="name_a").input("하루 우라라").run()
        self.assertEqual(list(app.dataframe[0].value["카드명"]), ["하루 우라라"])

    def test_confirmation_resets_after_any_export_change(self):
        with mock.patch.object(recognition, "models_ready", return_value=True):
            app = start_app([row("a", card=ASH), row("b", card=ASH)]).run()

            def confirm_and_check_enabled():
                app.checkbox(key="confirmed").check().run()
                self.assertEqual(download_buttons_disabled(app), [False, False])

            def assert_reset():
                self.assertFalse(app.checkbox(key="confirmed").value)
                self.assertEqual(download_buttons_disabled(app), [True, True])

            confirm_and_check_enabled()
            app.number_input(key="quantity_a").set_value(2).run()
            assert_reset()
            confirm_and_check_enabled()
            app.radio[0].set_value("영어").run()
            assert_reset()
            confirm_and_check_enabled()
            app.text_input(key="name_a").input("Ash").run()
            app.text_input(key="name_b").input("Ash").run()
            assert_reset()
            confirm_and_check_enabled()
            app.button(key="delete_b").click().run()
            assert_reset()
            confirm_and_check_enabled()
            app.run()  # a rerun without changes keeps the confirmation
            self.assertEqual(download_buttons_disabled(app), [False, False])


def png_bytes(color, size=(64, 48)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeRecognizer:
    """Stands in for the ONNX models; returns one card, nothing, or raises, per call."""

    outcomes = []

    def recognize(self, image, progress=None):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return [{"index": 1, "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]], "crop": image,
                 "candidates": CANDIDATES, "status": status, "detection_score": 0.9} for status in outcome]


class FakeMatcher:
    """Stands in for references.ReferenceMatcher; match() returns (or raises) the queued outcomes."""

    constructed = 0
    outcomes = []

    def __init__(self):
        type(self).constructed += 1

    def match(self, crop):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


LIBRARY = {"card_count": 102, "artwork_count": 130, "fingerprint": "library-v1"}
# A new card that exists only in Japanese so far: no Korean and no English official name.
# image_path is filled in with a real temporary file by ReferenceModeTest.setUp.
JA_ONLY_NEW = {"cid": 22691, "name_ko": None, "name_ja": "増殖するクリボー！", "name_en": None,
               "image_path": None, "source_url": "https://www.db.yugioh-card.com/"}


def reference_match(card, inliers=24):
    return {"card": card, "inliers": inliers, "inlier_ratio": 0.61, "spread": 0.42, "margin": 3.5}


class UploadTest(unittest.TestCase):
    def setUp(self):
        sort_patch = mock.patch.object(deck_order, "get_sort_keys", side_effect=app_sort_keys)
        sort_patch.start()
        self.addCleanup(sort_patch.stop)
        rarity_patch = mock.patch.object(rarities, "get_rarities", return_value=["N", "SR"])
        rarity_patch.start()
        self.addCleanup(rarity_patch.stop)
        st.cache_resource.clear()
        st.cache_data.clear()
        FakeMatcher.constructed = 0
        FakeMatcher.outcomes = []
        patches = [mock.patch.object(recognition, "models_ready", return_value=True),
                   mock.patch.object(recognition, "Recognizer", FakeRecognizer),
                   mock.patch.object(catalog, "resolve_card", return_value=ASH),
                   # These cases isolate model upload/lookup behavior; CandidateModeTest covers the new stage.
                   mock.patch.object(references, "needs_candidate_review", return_value=False),
                   mock.patch.object(references, "library_info", return_value=None),
                   mock.patch.object(references, "ReferenceMatcher", FakeMatcher)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def upload(self, app, *files):
        app.file_uploader[0].set_value([(name, data, "image/png") for name, data in files]).run()

    def recognize_button(self, app):
        return next(button for button in app.button if button.label == "카드 인식")

    def test_low_confidence_top_candidate_is_preselected_but_still_needs_review(self):
        # Codex: even 1% confidence should give the user a starting choice, not a blank row.
        candidates = [{**CANDIDATES[0], "score": 0.01}, {**LOCALIZED_CANDIDATES[1], "score": 0.005}]
        result = {"index": 1, "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]],
                  "crop": Image.new("RGB", (64, 48)), "candidates": candidates,
                  "status": "review", "detection_score": 0.9}
        with mock.patch.object(FakeRecognizer, "recognize", return_value=[result]), \
                mock.patch.object(catalog, "resolve_card", side_effect=lambda name: {
                    ASH["name_en"]: ASH, JA_ONLY["name_en"]: JA_ONLY}[name]):
            app = AppTest.from_file(APP_PATH, default_timeout=30).run()
            self.upload(app, ("deck.png", png_bytes("red")))
            self.recognize_button(app).click().run()
            self.assertFalse(app.exception)
            selected = app.session_state["rows"][0]
            self.assertEqual((selected["choice"], selected["card"], selected["status"]),
                             ("14558127", ASH, "review"))
            chooser = app.selectbox(key=f"choice_{selected['key']}")
            self.assertEqual(chooser.value, 1)
            self.assertEqual(chooser.options[1], "하루 우라라 (1%)")
            self.assertIn("확인 필요", chooser.label)
            self.assertTrue(any("자동 확정 0장 · 확인 필요 1장" in warning.value for warning in app.warning))
            self.assertEqual(len(app.success), 0)
            self.assertFalse(app.checkbox(key="confirmed").value)
            self.assertEqual(download_buttons_disabled(app), [True, True])
            app.checkbox(key="export_cid").check().run()
            self.assertEqual(list(app.dataframe[0].value["공식 CID"]), [12950])
            app.checkbox(key="confirmed").check().run()
            self.assertEqual(download_buttons_disabled(app), [False, False])

    def test_preselection_does_not_overwrite_a_manual_choice_or_clear_on_rerun(self):
        # Codex: default selection happens once; later explicit edits belong to the user.
        result = {"index": 1, "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]],
                  "crop": Image.new("RGB", (64, 48)), "candidates": LOCALIZED_CANDIDATES[:2],
                  "status": "review", "detection_score": 0.9}
        with mock.patch.object(FakeRecognizer, "recognize", return_value=[result]), \
                mock.patch.object(catalog, "resolve_card", side_effect=lambda name: {
                    ASH["name_en"]: ASH, JA_ONLY["name_en"]: JA_ONLY}[name]):
            app = AppTest.from_file(APP_PATH, default_timeout=30).run()
            self.upload(app, ("deck.png", png_bytes("red")))
            self.recognize_button(app).click().run()
            key = app.session_state["rows"][0]["key"]
            self.assertEqual(app.selectbox(key=f"choice_{key}").value, 1)
            app.selectbox(key=f"choice_{key}").set_value(2).run()
            app.radio[0].set_value("일본어").run()
            self.assertEqual(app.session_state["rows"][0]["choice"], "23434538")
            self.assertEqual(app.session_state["rows"][0]["card"]["cid"], 5050)
            app.selectbox(key=f"choice_{key}").set_value(0).run()
            app.run()
            self.assertFalse(app.exception)
            self.assertIsNone(app.session_state["rows"][0]["choice"])
            self.assertIsNone(app.session_state["rows"][0]["card"])
            self.assertEqual(download_buttons_disabled(app), [True, True])

    def test_failure_then_success_then_replaced_image_clears_output(self):
        FakeRecognizer.outcomes = [RuntimeError("onnx exploded"), ["recognized"]]
        app = AppTest.from_file(APP_PATH, default_timeout=30).run()
        self.upload(app, ("deck.png", png_bytes("red")))
        self.recognize_button(app).click().run()
        self.assertIn("onnx exploded", app.error[0].value)
        self.assertEqual(app.session_state["rows"], [])

        self.recognize_button(app).click().run()
        self.assertEqual(len(app.error), 0)
        self.assertEqual(len(app.session_state["rows"]), 1)
        self.assertEqual(app.session_state["rows"][0]["card"], ASH)

        self.upload(app, ("other.png", png_bytes("blue")))
        self.assertEqual(app.session_state["rows"], [])
        self.assertEqual(len(app.dataframe), 0)

    def test_low_confidence_is_not_auto_confirmed_nor_reported_as_success(self):
        FakeRecognizer.outcomes = [["recognized", "review"]]
        app = AppTest.from_file(APP_PATH, default_timeout=30).run()
        self.upload(app, ("deck.png", png_bytes("red")))
        self.recognize_button(app).click().run()
        review_row = app.session_state["rows"][1]
        self.assertEqual((review_row["choice"], review_row["card"], review_row["status"]),
                         ("14558127", ASH, "review"))
        self.assertEqual(len(app.success), 0)
        self.assertTrue(any("감지 2장 · 자동 확정 1장 · 확인 필요 1장" in w.value for w in app.warning))
        self.assertEqual(download_buttons_disabled(app), [True, True])

    def test_catalog_failure_keeps_partial_results_with_cause(self):
        FakeRecognizer.outcomes = [["recognized"]]
        app = AppTest.from_file(APP_PATH, default_timeout=30).run()
        self.upload(app, ("deck.png", png_bytes("red")))
        with mock.patch.object(catalog, "resolve_card", side_effect=catalog.CatalogError("HTTP 503")):
            self.recognize_button(app).click().run()
        self.assertEqual(len(app.session_state["rows"]), 1)
        self.assertEqual(len(app.success), 0)
        self.assertTrue(any("공식 DB 조회 실패 1장" in w.value for w in app.warning))
        self.assertTrue(any("HTTP 503" in w.value for w in app.warning))

    def test_retry_updates_summary_and_keeps_low_confidence_review(self):
        FakeRecognizer.outcomes = [["recognized", "review"]]
        app = AppTest.from_file(APP_PATH, default_timeout=30).run()
        self.upload(app, ("deck.png", png_bytes("red")))
        with mock.patch.object(catalog, "resolve_card", side_effect=catalog.CatalogError("offline")):
            self.recognize_button(app).click().run()
        self.assertTrue(any("공식 DB 조회 실패 2장" in warning.value for warning in app.warning))
        retry_key = "retry_" + app.session_state["rows"][1]["key"]
        app.button(key=retry_key).click().run()
        self.assertFalse(app.exception)
        self.assertTrue(any("감지 2장 · 자동 확정 1장 · 확인 필요 1장 · 공식 DB 조회 실패 0장" in warning.value
                            for warning in app.warning))
        self.assertFalse(any("공식 DB 조회 실패 2장" in warning.value for warning in app.warning))
        self.assertEqual(app.session_state["rows"][1]["status"], "review")
        self.assertEqual(download_buttons_disabled(app), [True, True])

    def test_all_confirmed_reports_success(self):
        FakeRecognizer.outcomes = [["recognized"]]
        app = AppTest.from_file(APP_PATH, default_timeout=30).run()
        self.upload(app, ("deck.png", png_bytes("red")))
        self.recognize_button(app).click().run()
        self.assertIn("자동 확정 1장", app.success[0].value)

    def test_no_detections_is_reported(self):
        FakeRecognizer.outcomes = [[]]
        app = AppTest.from_file(APP_PATH, default_timeout=30).run()
        self.upload(app, ("empty.png", png_bytes("red")))
        self.recognize_button(app).click().run()
        self.assertTrue(any("카드를 찾지 못했습니다" in w.value for w in app.warning))
        self.assertTrue(any("카드를 찾지 못한 사진: empty.png" in w.value for w in app.warning))
        self.assertEqual(len(app.success), 0)

    def test_oversized_and_corrupt_images_are_rejected(self):
        app = AppTest.from_file(APP_PATH, default_timeout=30).run()
        self.upload(app, ("broken.png", b"not an image"), ("huge.png", png_bytes("red", (8000, 6000))))
        errors = " ".join(error.value for error in app.error)
        self.assertIn("broken.png", errors)
        self.assertIn("huge.png", errors)
        self.assertTrue(self.recognize_button(app).proto.disabled)


class ReferenceModeTest(unittest.TestCase):
    def setUp(self):
        sort_patch = mock.patch.object(deck_order, "get_sort_keys", side_effect=app_sort_keys)
        sort_patch.start()
        self.addCleanup(sort_patch.stop)
        rarity_patch = mock.patch.object(rarities, "get_rarities", return_value=["N", "SR"])
        rarity_patch.start()
        self.addCleanup(rarity_patch.stop)
        st.cache_resource.clear()
        st.cache_data.clear()
        FakeMatcher.constructed = 0
        FakeMatcher.outcomes = []
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.reference_image = str(Path(temporary.name) / "reference.png")
        Path(self.reference_image).write_bytes(png_bytes("green"))
        self.ja_only = {**JA_ONLY_NEW, "image_path": self.reference_image}
        # Model-candidate lookups fail: anything that still works below did not need the network.
        self.resolve = mock.Mock(side_effect=catalog.CatalogError("offline"))
        self.library_info = mock.Mock(return_value=LIBRARY)
        patches = [mock.patch.object(recognition, "models_ready", return_value=True),
                   mock.patch.object(recognition, "Recognizer", FakeRecognizer),
                   mock.patch.object(catalog, "resolve_card", self.resolve),
                   # Isolate global-library behavior from the independent top-three comparison stage.
                   mock.patch.object(references, "needs_candidate_review", return_value=False),
                   mock.patch.object(references, "library_info", self.library_info),
                   mock.patch.object(references, "ReferenceMatcher", FakeMatcher)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def recognized_app(self, recognizer_outcomes, matcher_outcomes):
        FakeRecognizer.outcomes = recognizer_outcomes
        FakeMatcher.outcomes = matcher_outcomes
        app = AppTest.from_file(APP_PATH, default_timeout=30).run()
        app.file_uploader[0].set_value([("deck.png", png_bytes("red"), "image/png")]).run()
        next(button for button in app.button if button.label == "카드 인식").click().run()
        return app

    def recognize_button(self, app):
        return next(button for button in app.button if button.label == "카드 인식")

    def test_library_coverage_is_explicit_and_comparison_on_by_default(self):
        app = AppTest.from_file(APP_PATH, default_timeout=30).run()
        captions = " ".join(caption.value for caption in app.caption)
        self.assertIn("카드 102종 · 아트워크 130장", captions)
        self.assertIn("전체 유희왕 카드를 포함하지 않습니다", captions)
        self.assertTrue(app.toggle(key="use_references").value)

    def test_japanese_only_reference_match_is_selected_and_exported_without_lookup(self):
        app = self.recognized_app([["review"]], [reference_match(self.ja_only)])
        self.assertFalse(app.exception)
        self.assertEqual(FakeMatcher.constructed, 1)
        selected = app.session_state["rows"][0]
        reference = selected["candidates"][0]
        self.assertEqual((selected["choice"], selected["card"], selected["error"], selected["status"]),
                         ("official:22691", self.ja_only, None, "recognized"))
        self.assertEqual((reference["method"], reference["reference_card"], reference["inliers"],
                          reference["inlier_ratio"], reference["spread"], reference["margin"]),
                         ("reference", self.ja_only, 24, 0.61, 0.42, 3.5))
        self.assertNotIn("score", reference)
        self.assertEqual(selected["candidates"][1:], CANDIDATES)  # different identity: model proposal kept
        options = app.selectbox(key=f"choice_{selected['key']}").options
        self.assertEqual(options[1], "増殖するクリボー！ (공식 이미지 일치 · 특징점 24개)")
        self.assertTrue(any("공식 이미지 일치 1장" in w.value for w in [*app.success, *app.warning]))

        app.radio[0].set_value("일본어").run()
        app.checkbox(key="export_cid").check().run()
        table = app.dataframe[0].value
        self.assertEqual((list(table["카드명"]), list(table["공식 CID"])), (["増殖するクリボー！"], [22691]))
        self.assertEqual(download_buttons_disabled(app), [True, True])  # confirmation still required
        app.checkbox(key="confirmed").check().run()
        self.assertEqual(download_buttons_disabled(app), [False, False])
        # Only the model candidate's label looked anything up, and never with the reference card's names.
        for call in self.resolve.call_args_list:
            self.assertEqual(call.args, (ASH["name_en"],))

    def test_reference_replaces_model_candidate_of_same_identity(self):
        self.resolve.side_effect = None
        self.resolve.return_value = ASH
        official = {**ASH, "name_en": "ASH BLOSSOM  & Joyous Spring", "image_path": self.reference_image}
        model = CANDIDATES + [{"card_id": "1", "name_en": "Other A", "name_ja": None, "score": 0.1},
                              {"card_id": "2", "name_en": "Other B", "name_ja": None, "score": 0.1},
                              {"card_id": "3", "name_en": "Other C", "name_ja": None, "score": 0.1}]
        with mock.patch.object(FakeRecognizer, "recognize", lambda self, image, progress=None: [
                {"index": 1, "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]], "crop": image,
                 "candidates": model, "status": "review", "detection_score": 0.9}]):
            app = self.recognized_app([], [reference_match(official, inliers=31)])
        candidates = app.session_state["rows"][0]["candidates"]
        self.assertEqual([c["card_id"] for c in candidates], ["official:12950", "1", "2"])
        self.assertEqual(app.selectbox(key=f"choice_{app.session_state['rows'][0]['key']}").options[1],
                         "하루 우라라 (공식 이미지 일치 · 특징점 31개)")
        self.assertIn("공식 이미지 일치 1장", app.success[0].value)

    def test_unmatched_crop_keeps_model_result_and_review(self):
        app = self.recognized_app([["review", "recognized"]], [None, None])
        unmatched, recognized = app.session_state["rows"]
        self.assertEqual((unmatched["candidates"], unmatched["status"], unmatched["choice"], unmatched["card"]),
                         (CANDIDATES, "review", CANDIDATES[0]["card_id"], None))
        self.assertIn("offline", unmatched["error"])  # selected proposal does not conceal a failed lookup
        self.assertEqual(app.selectbox(key=f"choice_{unmatched['key']}").value, 1)
        self.assertEqual((recognized["candidates"], recognized["status"], recognized["choice"]),
                         (CANDIDATES, "recognized", CANDIDATES[0]["card_id"]))
        self.assertIn("offline", recognized["error"])  # normal model lookup path, failure kept visible
        self.assertEqual(download_buttons_disabled(app), [True, True])

    def test_matcher_failure_aborts_recognition_and_clears_stale_export(self):
        app = self.recognized_app([["review"]], [reference_match(self.ja_only)])
        app.radio[0].set_value("일본어").run()
        app.checkbox(key="confirmed").check().run()
        self.assertEqual(download_buttons_disabled(app), [False, False])

        FakeRecognizer.outcomes = [["review"]]
        FakeMatcher.outcomes = [RuntimeError("reference index corrupt")]
        self.recognize_button(app).click().run()
        self.assertIn("reference index corrupt", app.error[0].value)
        self.assertEqual(app.session_state["rows"], [])
        self.assertEqual(download_buttons_disabled(app), [True, True])

        FakeRecognizer.outcomes = [["review"]]
        with mock.patch.object(references, "ReferenceMatcher", side_effect=OSError("library unreadable")):
            self.recognize_button(app).click().run()
        self.assertIn("library unreadable", app.error[0].value)
        self.assertEqual(app.session_state["rows"], [])

    def test_reference_without_any_official_name_is_rejected(self):
        nameless = {**self.ja_only, "name_ja": None}
        app = self.recognized_app([["review"]], [reference_match(nameless)])
        self.assertIn("공식 이름이 하나도 없습니다", app.error[0].value)
        self.assertEqual(app.session_state["rows"], [])

    def test_invalid_library_blocks_reference_mode_until_user_turns_it_off(self):
        self.library_info.side_effect = ValueError("manifest sha256 mismatch")
        FakeRecognizer.outcomes = [["review"]]
        app = AppTest.from_file(APP_PATH, default_timeout=30).run()
        app.file_uploader[0].set_value([("deck.png", png_bytes("red"), "image/png")]).run()
        self.assertIn("manifest sha256 mismatch", app.error[0].value)
        self.assertTrue(app.toggle(key="use_references").value)
        self.assertTrue(self.recognize_button(app).proto.disabled)

        app.toggle(key="use_references").set_value(False).run()
        self.recognize_button(app).click().run()
        self.assertEqual(FakeMatcher.constructed, 0)
        self.assertEqual(app.session_state["rows"][0]["candidates"], CANDIDATES)

    def test_absent_library_says_not_initialized_and_defaults_to_basic_mode(self):
        self.library_info.return_value = None
        app = self.recognized_app([["review"]], [])
        self.assertTrue(any("초기화되지 않았습니다" in info.value for info in app.info))
        self.assertFalse(app.toggle(key="use_references").value)
        self.assertEqual(FakeMatcher.constructed, 0)
        self.assertEqual(app.session_state["rows"][0]["candidates"], CANDIDATES)

    def test_library_disappearing_keeps_selected_mode_and_blocks_recognition(self):
        app = self.recognized_app([["review"]], [reference_match(self.ja_only)])
        app.radio[0].set_value("일본어").run()
        app.checkbox(key="confirmed").check().run()

        self.library_info.return_value = None  # e.g. data/references deleted while the app is open
        app.run()
        self.assertTrue(app.toggle(key="use_references").value)  # not silently switched to basic
        self.assertTrue(any("초기화되지 않았습니다" in info.value for info in app.info))
        self.assertTrue(self.recognize_button(app).proto.disabled)
        self.assertEqual(app.session_state["rows"], [])  # stale reference results are gone
        self.assertEqual(download_buttons_disabled(app), [True, True])

        app.toggle(key="use_references").set_value(False).run()  # explicit choice of basic mode
        FakeRecognizer.outcomes = [["review"]]
        self.recognize_button(app).click().run()
        self.assertEqual(FakeMatcher.constructed, 1)  # only the first, reference-mode recognition
        self.assertEqual(app.session_state["rows"][0]["candidates"], CANDIDATES)

    def test_changing_mode_or_library_resets_results_and_confirmation(self):
        app = self.recognized_app([["review"]], [reference_match(self.ja_only)])
        app.radio[0].set_value("일본어").run()
        app.checkbox(key="confirmed").check().run()
        app.toggle(key="use_references").set_value(False).run()
        self.assertEqual(app.session_state["rows"], [])
        self.assertFalse(app.checkbox(key="confirmed").value)

        app.toggle(key="use_references").set_value(True).run()
        FakeRecognizer.outcomes = [["review"]]
        FakeMatcher.outcomes = [reference_match(self.ja_only)]
        self.recognize_button(app).click().run()
        self.assertEqual(len(app.session_state["rows"]), 1)
        app.checkbox(key="confirmed").check().run()
        self.library_info.return_value = {**LIBRARY, "fingerprint": "library-v2"}
        app.run()
        self.assertEqual(app.session_state["rows"], [])
        self.assertFalse(app.checkbox(key="confirmed").value)
        self.assertEqual(download_buttons_disabled(app), [True, True])


if __name__ == "__main__":
    unittest.main()
