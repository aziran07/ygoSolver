"""User-facing rarity acceptance, independent of crawler implementation."""

import unittest
from unittest import mock

import streamlit as st

import catalog
import rarities
import recognition
import references
from test_app import ASH, JA_ONLY, CANDIDATES, row, start_app, download_buttons_disabled


class RarityAppTest(unittest.TestCase):
    def setUp(self):
        st.cache_resource.clear()
        st.cache_data.clear()
        self.options = mock.Mock(side_effect=lambda cid: {12950: ["N", "SR", "UR"], 5050: ["SR", "UR"]}[cid])
        for patch in (mock.patch.object(recognition, "models_ready", return_value=True),
                      mock.patch.object(references, "library_info", return_value=None),
                      mock.patch.object(rarities, "get_rarities", self.options)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_existing_rows_default_to_lowest_available_and_preserve_user_edits(self):
        first = row("a", card=ASH, quantity=3)
        first["name_override"] = "내 우라라"
        app = start_app([first, row("b", card=JA_ONLY)]).run()
        self.assertFalse(app.exception)
        self.assertEqual(app.selectbox(key="rarity_a").value, "N")
        self.assertEqual(app.selectbox(key="rarity_b").value, "SR")
        self.assertEqual(app.selectbox(key="rarity_b").options, ["슈퍼 레어", "울트라 레어"])
        self.assertEqual(app.text_input(key="name_a").value, "내 우라라")
        self.assertEqual(app.number_input(key="quantity_a").value, 3)
        app.selectbox(key="rarity_a").set_value("UR").run()
        app.number_input(key="quantity_a").set_value(4).run()
        app.radio[0].set_value("영어").run()
        app.run()
        self.assertEqual(app.selectbox(key="rarity_a").value, "UR")
        self.assertEqual(self.options.call_count, 2)

    def test_distinct_rarity_rows_and_confirmation_reset(self):
        app = start_app([row("a", card=ASH), row("b", card=ASH, quantity=2)]).run()
        self.assertEqual(list(app.dataframe[0].value["수량"]), [3])
        app.checkbox(key="confirmed").check().run()
        self.assertEqual(download_buttons_disabled(app), [False, False])
        app.selectbox(key="rarity_b").set_value("SR").run()
        self.assertFalse(app.exception)
        self.assertEqual(list(app.dataframe[0].value["수량"]), [1, 2])
        self.assertEqual(list(app.dataframe[0].value["레어도"]), ["노멀", "슈퍼 레어"])
        self.assertFalse(app.checkbox(key="confirmed").value)
        self.assertEqual(download_buttons_disabled(app), [True, True])
        self.assertTrue(any("총 3장" in text.value and "1종" in text.value for text in app.markdown))
        app.checkbox(key="confirmed").check().run()
        self.assertEqual(download_buttons_disabled(app), [False, False])

    def test_candidate_change_resets_to_lowest_even_if_previous_rarity_also_exists(self):
        candidates = [*CANDIDATES, {"card_id": "222", "name_en": JA_ONLY["name_en"], "score": .2}]
        first = row("a", card=ASH, candidates=candidates)
        first["choice"] = CANDIDATES[0]["card_id"]
        with mock.patch.object(catalog, "resolve_card", side_effect=lambda name: ASH if name == ASH["name_en"] else JA_ONLY):
            app = start_app([first]).run()
            app.selectbox(key="rarity_a").set_value("UR").run()
            app.selectbox(key="choice_a").set_value(2).run()
            self.assertEqual(app.selectbox(key="rarity_a").value, "SR")
            self.assertEqual(app.session_state["rows"][0]["card"]["cid"], 5050)
            app.selectbox(key="choice_a").set_value(0).run()
            self.assertEqual(download_buttons_disabled(app), [True, True])
            app.selectbox(key="choice_a").set_value(1).run()
            self.assertEqual(app.selectbox(key="rarity_a").value, "N")

    def test_failed_rarity_query_is_visible_cached_and_explicit_retry_recovers(self):
        self.options.side_effect = RuntimeError("rarity source HTTP 503")
        first = row("a", card=ASH, quantity=4)
        first["name_override"] = "수정 이름"
        app = start_app([first]).run()
        app.run()
        self.assertFalse(app.exception)
        self.assertEqual(self.options.call_count, 1)
        self.assertTrue(any("HTTP 503" in item.value for item in [*app.warning, *app.error]))
        self.assertEqual(download_buttons_disabled(app), [True, True])
        self.assertEqual(app.session_state["rows"][0]["card"]["cid"], 12950)
        self.options.side_effect = None
        self.options.return_value = ["SR", "UR"]
        app.button(key="retry_rarity_a").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(self.options.call_count, 2)
        self.assertEqual(app.selectbox(key="rarity_a").value, "SR")
        self.assertEqual(app.number_input(key="quantity_a").value, 4)
        self.assertEqual(app.text_input(key="name_a").value, "수정 이름")
        self.assertFalse(any("HTTP 503" in item.value for item in [*app.warning, *app.error]))

    def test_no_ocg_rarity_blocks_export_without_inventing_normal(self):
        self.options.side_effect = None
        self.options.return_value = []
        app = start_app([row("a", card=ASH)]).run()
        self.assertFalse(app.exception)
        self.assertEqual(download_buttons_disabled(app), [True, True])
        self.assertFalse(app.dataframe)
        for selectbox in app.selectbox:
            self.assertNotIn("노멀", selectbox.options)

    def test_confirmed_no_ocg_printing_is_explained_without_pointless_retry(self):
        self.options.side_effect = rarities.RarityUnavailable("CID 12950 has no Korean or Japanese printing")
        app = start_app([row("a", card=ASH)]).run()
        self.assertFalse(app.exception)
        self.assertEqual(download_buttons_disabled(app), [True, True])
        self.assertTrue(any("한국어" in item.value and "일본어" in item.value for item in app.warning))
        self.assertFalse([button for button in app.button if button.key == "retry_rarity_a"])

    def test_catalog_retry_for_another_candidate_preserves_same_cid_rarity(self):
        candidates = [*CANDIDATES, {"card_id": "222", "name_en": JA_ONLY["name_en"], "score": .2}]
        first = row("a", card=ASH, candidates=candidates, quantity=3)
        first["choice"] = CANDIDATES[0]["card_id"]
        def lookup(name):
            if name == ASH["name_en"]:
                return ASH
            raise catalog.CatalogError("candidate offline")
        with mock.patch.object(catalog, "resolve_card", side_effect=lookup) as resolve:
            app = start_app([first]).run()
            app.selectbox(key="rarity_a").set_value("UR").run()
            resolve.side_effect = lambda name: ASH if name == ASH["name_en"] else JA_ONLY
            app.button(key="retry_a").click().run()
        self.assertFalse(app.exception)
        self.assertEqual(app.selectbox(key="rarity_a").value, "UR")
        self.assertEqual(app.number_input(key="quantity_a").value, 3)

    def test_manual_add_gets_rarity_dropdown_and_lowest_default(self):
        with mock.patch.object(catalog, "resolve_card", return_value=ASH):
            app = start_app([]).run()
            next(item for item in app.text_input if item.label == "영어 카드명").set_value(ASH["name_en"])
            next(button for button in app.button if button.label == "추가").click().run()
        self.assertFalse(app.exception)
        added = app.session_state["rows"][0]
        self.assertEqual(app.selectbox(key=f"rarity_{added['key']}").value, "N")

    def test_invalid_saved_rarity_stays_unresolved_until_user_selects_valid_option(self):
        first = row("a", card=ASH)
        first.update(rarity_cid=12950, rarity="SE")
        app = start_app([first]).run()
        self.assertFalse(app.exception)
        self.assertIsNone(app.selectbox(key="rarity_a").value)
        self.assertEqual(download_buttons_disabled(app), [True, True])
        self.assertTrue(any("SE" in item.value for item in app.warning))
        app.selectbox(key="rarity_a").set_value("SR").run()
        self.assertFalse(app.exception)
        self.assertEqual(list(app.dataframe[0].value["레어도"]), ["슈퍼 레어"])


if __name__ == "__main__":
    unittest.main()
