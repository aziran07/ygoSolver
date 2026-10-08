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
import price_collector
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
        self.collection_state = {"state": "wait", "next_allowed_at": datetime.now(timezone.utc) + timedelta(hours=9),
                                 "last_attempt": None}
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
        patch = mock.patch.object(price_collector, "collect_card_number", side_effect=lambda *args, **kwargs: self.collection_state)
        self.collect = patch.start()
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

    def test_rarity_names_and_separate_unit_price_keep_aggregated_export_total(self):
        app = self.start([row("one", 2), row("two", 3)])
        self.assertEqual(app.selectbox(key="rarity_one").options, ["노멀", "울트라 레어"])
        self.assertEqual([(m.label, m.value) for m in app.metric],
                         [("단가 (일본판)", "240원"), ("단가 (일본판)", "240원")])
        self.assertEqual(len(app.expander), 2)
        table = app.dataframe[0].value
        self.assertEqual(list(table["단가(원)"]), [240])
        self.assertEqual(list(table["합계(원)"]), [1200])
        self.assertEqual(list(table["가격 수록 번호"]), ["15AY-JPB22"])
        self.assertEqual(self.fetch.call_count, 1)
        self.collect.assert_not_called()
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
        self.assertEqual(list(app.dataframe[0].value["가격 상태"]), ["collection_wait"])
        self.assertTrue(app.dataframe[0].value["단가(원)"].isna().all())
        self.assertEqual(app.selectbox(key="rarity_one").options, ["노멀", "울트라 레어"])
        self.assertEqual(app.metric[0].value, "수집 대기")
        self.assert_downloads(app, True)

    def test_missing_prices_attempt_collection_and_show_wait_not_zero_or_sold_out(self):
        self.failure = price_store.PriceNotFound("not captured")
        app = self.start()
        self.assertEqual(app.metric[0].value, "수집 대기")
        self.assertTrue(app.dataframe[0].value["단가(원)"].isna().all())
        self.assertEqual(list(app.dataframe[0].value["가격 상태"]), ["collection_wait"])
        self.assertEqual(self.collect.call_count, 1)
        self.assertEqual(self.collect.call_args.args[:2], ("15AY-JPB22", "ja"))
        visible = " ".join(e.value for kind in ("caption", "info", "warning", "markdown")
                           for e in app.get(kind))
        self.assertIn("수집", visible)

    def test_separate_field_distinguishes_stock_expiry_and_backend_failures(self):
        cases = [("out_of_stock", None, "no_stock"),
                 ("in_stock", price_store.StalePriceError("expired proof"), "collection_wait"),
                 ("in_stock", price_store.PriceDatabaseError("db proof"), "database_error"),
                 ("in_stock", price_store.PriceCacheError("cache proof"), "cache_error")]
        seen = set()
        for stock, failure, status in cases:
            with self.subTest(status=status):
                self.stock, self.failure = stock, failure
                app = self.start()
                self.assertEqual(list(app.dataframe[0].value["가격 상태"]), [status])
                self.assertTrue(app.dataframe[0].value["단가(원)"].isna().all())
                self.assertEqual(app.selectbox(key="rarity_one").options, ["노멀", "울트라 레어"])
                self.assertNotEqual(app.metric[0].value, "가격 미수집")
                seen.add(app.metric[0].value)
        self.assertEqual(len(seen), 4)

    def test_locale_is_independent_of_export_name_language(self):
        app = self.start()
        app.checkbox(key="confirmed").check().run()
        app.radio(key="price_locale").set_value("한국판").run()
        self.assertFalse(app.exception)
        self.assertFalse(app.checkbox(key="confirmed").value)
        self.assertEqual(list(app.dataframe[0].value["카드명"]), [NAME_KO])
        self.assertEqual(list(app.dataframe[0].value["가격 판본"]), ["ko"])
        self.assertEqual(list(app.dataframe[0].value["가격 상태"]), ["collection_wait"])
        self.assertEqual(app.metric[0].label, "단가 (한국판)")

    def test_price_change_and_expiry_each_reset_confirmation_without_losing_card(self):
        app = self.start()
        app.checkbox(key="confirmed").check().run()
        self.cost = 300
        app.run()
        self.assertFalse(app.checkbox(key="confirmed").value)
        self.assertEqual(list(app.dataframe[0].value["단가(원)"]), [300])
        self.assertEqual(app.metric[0].value, "300원")
        self.assertEqual(app.selectbox(key="rarity_one").value, "N")
        self.assertEqual(app.selectbox(key="rarity_one").options, ["노멀", "울트라 레어"])
        app.checkbox(key="confirmed").check().run()
        self.failure = price_store.StalePriceError("twelve hours passed")
        app.run()
        self.assertFalse(app.exception)
        self.assertFalse(app.checkbox(key="confirmed").value)
        self.assertEqual(list(app.dataframe[0].value["가격 상태"]), ["collection_wait"])
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
        self.collect.assert_not_called()
        self.assertEqual(list(app.dataframe[0].value["가격 상태"]), ["config_error"])

    def test_missing_price_is_collected_saved_and_displayed_in_same_run(self):
        self.failure = price_store.PriceNotFound("missing initially")
        def stored(*args, **kwargs):
            self.failure = None
            self.cost = 520
            return {"state": "stored", "snapshot_id": 20, "product_count": 1}
        self.collect.side_effect = stored
        app = self.start()
        self.assertEqual(app.metric[0].value, "520원")
        self.assertEqual(list(app.dataframe[0].value["가격 상태"]), ["ok"])
        self.assertEqual(list(app.dataframe[0].value["단가(원)"]), [520])
        self.assertEqual(self.collect.call_count, 1)
        app.checkbox(key="confirmed").check().run()
        self.assertFalse(app.exception)
        self.assertTrue(app.checkbox(key="confirmed").value)
        self.assert_downloads(app, False)
        self.assertEqual(self.collect.call_count, 1)

    def test_failed_collection_is_visible_and_never_exports_old_amount(self):
        self.failure = price_store.StalePriceError("old")
        self.collection_state = {"state": "failed", "error": "HTTP 429 proof",
                                 "attempted_at": datetime.now(timezone.utc),
                                 "next_allowed_at": datetime.now(timezone.utc) + timedelta(hours=12)}
        app = self.start()
        self.assertEqual(app.metric[0].value, "수집 실패")
        self.assertEqual(list(app.dataframe[0].value["가격 상태"]), ["collection_failed"])
        self.assertTrue(app.dataframe[0].value["단가(원)"].isna().all())
        self.assertIn("429", app.dataframe[0].value["가격 상세"].iloc[0])

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
