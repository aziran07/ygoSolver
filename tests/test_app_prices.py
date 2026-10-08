"""Codex UI tests for user's rarity/price and export workflow."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from streamlit.testing.v1 import AppTest
from streamlit.delta_generator import DeltaGenerator
import card_prices
import catalog
import deck_order
import price_store
import rarities
import recognition
import references
from price_test_fixtures import CID, NAME_JA, NAME_KO, group, product

APP_PATH = str(Path(__file__).resolve().parents[1] / "app.py")
CARD = {"cid": CID, "name_ko": NAME_KO, "name_ja": NAME_JA,
        "name_en": "Diffusion Wave-Motion", "image_path": None, "source_url": catalog.detail_url(CID, "en")}


def row(key="one", quantity=1):
    return {"key": key, "source": "manual", "image_name": None, "index": None, "crop": None,
            "candidates": [], "status": "manual", "choice": None, "card": dict(CARD),
            "error": None, "quantity": quantity, "name_override": ""}


class PriceAppTest(unittest.TestCase):
    def setUp(self):
        self.cost = 240
        self.failure = None
        self.stock = "in_stock"
        self.observed_at = product()["observed_at"]
        patches = [
            mock.patch.dict(os.environ, {"DATABASE_URL": "postgresql://example", "REDIS_URL": "redis://example"}),
            mock.patch.object(recognition, "models_ready", return_value=True),
            mock.patch.object(references, "library_info", return_value=None),
            mock.patch.object(rarities, "get_rarities", return_value=["N", "UR"]),
            mock.patch.object(deck_order, "get_sort_keys", return_value={CID: (2, 1)}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.prints_patch = mock.patch.object(card_prices, "fetch_official_prints", side_effect=lambda cid, locale:
            [{"pid": 1, "card_number": "15AY-JPB22" if locale == "ja" else "SD6-KR030", "rid": 1},
             {"pid": 2, "card_number": "EE1-JP162" if locale == "ja" else "SD6-KR031", "rid": 4}])
        self.fetch = self.prints_patch.start()
        self.addCleanup(self.prints_patch.stop)
        patch = mock.patch.object(price_store, "get_prices", side_effect=self.read_prices)
        self.read = patch.start()
        self.addCleanup(patch.stop)

    def read_prices(self, number, locale, database_url, redis_url):
        if self.failure:
            raise self.failure
        if locale == "ko" or number == "EE1-JP162":
            raise price_store.PriceNotFound("not captured")
        return group(number, [product(price=self.cost, stock=self.stock, observed_at=self.observed_at)])

    def start(self, rows=None):
        app = AppTest.from_file(APP_PATH, default_timeout=20)
        app.session_state["rows"] = [row()] if rows is None else rows
        app.run()
        self.assertFalse(app.exception)
        return app

    def assert_downloads(self, app, disabled):
        self.assertEqual([b.proto.disabled for b in app.get("download_button")], [disabled, disabled])

    def test_rarity_dropdown_shows_price_and_export_table_keeps_aggregated_total(self):
        app = self.start([row("one", 2), row("two", 3)])
        self.assertTrue(any("240" in label for label in app.selectbox(key="rarity_one").options))
        table = app.dataframe[0].value
        self.assertEqual(list(table["단가(원)"]), [240])
        self.assertEqual(list(table["합계(원)"]), [1200])
        self.assertEqual(list(table["가격 수록 번호"]), ["15AY-JPB22"])
        self.assertEqual(self.fetch.call_count, 1)
        self.assert_downloads(app, True)
        app.checkbox(key="confirmed").check().run()
        self.assertFalse(app.exception)
        self.assert_downloads(app, False)

    def test_rarity_switch_never_reuses_previous_price_and_resets_confirmation(self):
        app = self.start()
        app.checkbox(key="confirmed").check().run()
        app.selectbox(key="rarity_one").set_value("UR").run()
        self.assertFalse(app.exception)
        self.assertFalse(app.checkbox(key="confirmed").value)
        self.assertEqual(list(app.dataframe[0].value["가격 상태"]), ["not_observed"])
        self.assertTrue(app.dataframe[0].value["단가(원)"].isna().all())
        self.assert_downloads(app, True)

    def test_locale_is_independent_of_export_name_language(self):
        app = self.start()
        app.checkbox(key="confirmed").check().run()
        app.radio(key="price_locale").set_value("한국판").run()
        self.assertFalse(app.exception)
        self.assertFalse(app.checkbox(key="confirmed").value)
        self.assertEqual(list(app.dataframe[0].value["카드명"]), [NAME_KO])
        self.assertEqual(list(app.dataframe[0].value["가격 판본"]), ["ko"])
        self.assertEqual(list(app.dataframe[0].value["가격 상태"]), ["not_observed"])

    def test_price_change_and_expiry_each_reset_confirmation_without_losing_card(self):
        app = self.start()
        app.checkbox(key="confirmed").check().run()
        self.cost = 300
        app.run()
        self.assertFalse(app.checkbox(key="confirmed").value)
        self.assertEqual(list(app.dataframe[0].value["단가(원)"]), [300])
        app.checkbox(key="confirmed").check().run()
        self.failure = price_store.StalePriceError("twelve hours passed")
        app.run()
        self.assertFalse(app.exception)
        self.assertFalse(app.checkbox(key="confirmed").value)
        self.assertEqual(list(app.dataframe[0].value["가격 상태"]), ["expired"])
        self.assertEqual(list(app.dataframe[0].value["카드명"]), [NAME_KO])
        self.assertTrue(app.dataframe[0].value["단가(원)"].isna().all())
        app.checkbox(key="confirmed").check().run()
        self.assert_downloads(app, False)

    def test_mapping_failure_is_visible_exported_and_explicit_retry_recovers(self):
        self.fetch.side_effect = card_prices.OfficialPrintError("wrong CID evidence")
        app = self.start()
        self.assertEqual(list(app.dataframe[0].value["가격 상태"]), ["official_error"])
        self.assertIn("wrong CID evidence", app.dataframe[0].value["가격 상세"].iloc[0])
        self.fetch.side_effect = None
        self.fetch.return_value = [{"pid": 1, "card_number": "15AY-JPB22", "rid": 1}]
        retries = [b for b in app.button if "수록" in b.label and ("다시" in b.label or "재" in b.label)]
        self.assertEqual(len(retries), 1)
        retries[0].click().run()
        self.assertFalse(app.exception)
        self.assertEqual(list(app.dataframe[0].value["가격 상태"]), ["ok"])

    def test_missing_config_does_not_trigger_official_or_shop_queries(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            app = self.start()
        self.fetch.assert_not_called()
        self.read.assert_not_called()
        self.assertEqual(list(app.dataframe[0].value["가격 상태"]), ["config_error"])

    def test_actual_download_callback_rejects_price_that_expired_after_confirmation(self):
        callbacks = {}
        original = DeltaGenerator.download_button

        def capture(container, label, data, *args, **kwargs):
            if callable(data):
                callbacks[kwargs["file_name"]] = data
            return original(container, label, data, *args, **kwargs)

        with mock.patch.object(DeltaGenerator, "download_button", capture):
            app = self.start()
            app.checkbox(key="confirmed").check().run()
        self.assertEqual(set(callbacks), {"deck.csv", "deck.xlsx"})
        self.assertTrue(callbacks["deck.csv"]().startswith(b"\xef\xbb\xbf"))
        future = datetime.fromisoformat(self.observed_at) + timedelta(hours=12)
        for build in callbacks.values():
            with mock.patch.dict(build.__globals__, {"datetime": SimpleNamespace(now=lambda tz: future)}):
                with self.assertRaisesRegex(ValueError, "만료"):
                    build()


if __name__ == "__main__":
    unittest.main()
