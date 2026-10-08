"""Codex acceptance: bind official identity and choose only a fresh in-stock minimum."""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import card_prices
import catalog
import price_store
from price_test_fixtures import CID, NAME_JA, detail_html, product, group

PRINTS = [{"pid": 3315001, "card_number": "15AY-JPB22", "rid": 1},
          {"pid": 2201013, "card_number": "SD6-JP030", "rid": 1},
          {"pid": 3311000, "card_number": "EE1-JP162", "rid": 4}]


class OfficialMappingTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db = Path(temporary.name) / "official.sqlite"
        with sqlite3.connect(self.db) as db:
            db.executescript('''
                CREATE TABLE cards (cid INTEGER PRIMARY KEY, name_ko TEXT, name_ja TEXT);
                INSERT INTO cards VALUES (5631, '확산하는 파동', '拡散する波動');
                CREATE TABLE rarity_prints (locale TEXT, pid INTEGER, cid INTEGER, rid INTEGER, code TEXT);
                INSERT INTO rarity_prints VALUES ('ja',3315001,5631,1,'N'),
                  ('ja',2201013,5631,1,'N'), ('ja',3311000,5631,4,'UR');
            ''')

    def fetch(self, html=None, url=None):
        session = mock.Mock()
        response = session.get.return_value
        response.text = detail_html() if html is None else html
        response.content = response.text.encode("utf-8")
        response.url = catalog.detail_url(CID, "ja") if url is None else url
        return card_prices.fetch_official_prints(CID, "ja", database_path=self.db, session=session)

    def test_verified_identity_retains_print_numbers_and_finishes(self):
        actual = self.fetch()
        self.assertEqual(sorted((p["card_number"], p["rid"]) for p in actual),
                         [("15AY-JPB22", 1), ("EE1-JP162", 4), ("SD6-JP030", 1)])

    def test_same_name_wrong_cid_locale_redirect_or_snapshot_mismatch_rejected(self):
        cases = [dict(html=detail_html(cid=4007)), dict(html=detail_html(locale="ko")),
                 dict(html=detail_html(name="Different Card")),
                 dict(url=catalog.detail_url(4007, "ja")),
                 dict(html=detail_html(prints=[(3315001, "15AY-JPB22", "N", 1)]))]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(card_prices.OfficialPrintError):
                self.fetch(**kwargs)

    def test_unknown_cid_is_not_resolved_from_a_shop_name(self):
        with sqlite3.connect(self.db) as db:
            db.execute("DELETE FROM cards")
        with self.assertRaises(card_prices.OfficialPrintError):
            self.fetch()


class MinimumPriceTest(unittest.TestCase):
    def setUp(self):
        self.groups = {
            "15AY-JPB22": group("15AY-JPB22", [product(price=240),
                product("2", price=10, stock="out_of_stock"),
                product("3", price=1, stock="unknown"),
                product("4", price=30, rarity="UR OverFrame")]),
            "SD6-JP030": group("SD6-JP030", [product("5", number="SD6-JP030", price=300)]),
        }

    def quote(self, rarity="N", locale="ja", prints=PRINTS, database_url="postgresql://test", redis_url="redis://test"):
        def read(number, requested_locale, database_url, redis_url):
            self.assertEqual(requested_locale, locale)
            result = self.groups.get(number)
            if result is None:
                raise price_store.PriceNotFound("not captured")
            if isinstance(result, Exception):
                raise result
            return result
        with mock.patch.object(price_store, "get_prices", side_effect=read):
            return card_prices.get_card_price(CID, rarity, locale, prints, database_url, redis_url)

    def test_minimum_excludes_cheaper_sold_out_unknown_and_other_finish(self):
        q = self.quote()
        self.assertEqual((q["status"], q["unit_price_krw"], q["card_number"], q["product_id"]),
                         ("ok", 240, "15AY-JPB22", "39577"))
        self.assertEqual(q["in_stock_products"], 2)
        self.assertGreaterEqual(q["excluded_unverified"], 1)

    def test_minimum_is_across_print_numbers_and_independent_of_input_order(self):
        self.groups["SD6-JP030"] = group("SD6-JP030", [product("5", "SD6-JP030", 180)])
        for prints in (PRINTS, PRINTS[::-1]):
            q = self.quote(prints=prints)
            self.assertEqual((q["unit_price_krw"], q["card_number"]), (180, "SD6-JP030"))

    def test_no_stock_is_blank_not_zero_or_sold_out_price(self):
        self.groups = {"15AY-JPB22": group("15AY-JPB22", [product(stock="out_of_stock")])}
        q = self.quote()
        self.assertEqual(q["status"], "no_stock")
        self.assertIsNone(q["unit_price_krw"])

    def test_uncollected_and_unverified_are_distinct(self):
        self.groups = {}
        self.assertEqual(self.quote()["status"], "not_observed")
        self.groups = {"15AY-JPB22": group("15AY-JPB22", [product(rarity="Mystery Rare")])}
        self.assertEqual(self.quote()["status"], "unverified")

    def test_wrong_rarity_and_locale_never_get_normal_japanese_price(self):
        q = self.quote(rarity="UR")
        self.assertEqual(q["status"], "not_observed")
        self.assertIsNone(q["unit_price_krw"])
        q = self.quote(locale="ko", prints=[{"pid": 10, "card_number": "SD6-KR030", "rid": 1}])
        self.assertEqual(q["status"], "not_observed")
        self.assertIsNone(q["unit_price_krw"])

    def test_generic_secret_cannot_choose_between_two_official_finishes(self):
        prints = [{"pid": 1, "card_number": "15AY-JPB22", "rid": rid} for rid in (5, 43)]
        self.groups = {"15AY-JPB22": group("15AY-JPB22", [product(rarity="Secret Rare")])}
        for rarity in ("SE", "SE@43"):
            q = self.quote(rarity=rarity, prints=prints)
            self.assertEqual(q["status"], "unverified")
            self.assertIsNone(q["unit_price_krw"])

    def test_failure_in_one_print_does_not_produce_partial_minimum(self):
        cases = [(price_store.StalePriceError("expired evidence"), "expired"),
                 (price_store.PriceError("broken evidence"), "data_error")]
        for error, status in cases:
            self.groups["SD6-JP030"] = error
            q = self.quote()
            self.assertEqual(q["status"], status)
            self.assertIsNone(q["unit_price_krw"])
            self.assertIn(str(error), q["detail"])

    def test_earlier_quote_expiring_during_later_query_is_not_returned(self):
        now = datetime.now(timezone.utc)
        old = product(observed_at=(now - timedelta(hours=12, seconds=1)).isoformat())
        self.groups["15AY-JPB22"] = group("15AY-JPB22", [old])
        self.assertEqual(self.quote()["status"], "expired")

    def test_missing_configuration_and_unqueryable_official_print_are_explicit(self):
        self.assertEqual(self.quote(database_url=None)["status"], "config_error")
        self.assertEqual(self.quote(prints=[])["status"], "no_edition_print")
        self.assertEqual(self.quote(prints=[{"pid": 1, "card_number": "303-053", "rid": 1}])["status"],
                         "no_card_number")

    def test_invalid_identity_arguments_fail_before_query(self):
        for cid, rarity, locale in ((True, "N", "ja"), (0, "N", "ja"), (CID, "BAD", "ja"), (CID, "N", "en")):
            with self.subTest(args=(cid, rarity, locale)), self.assertRaises(ValueError):
                card_prices.get_card_price(cid, rarity, locale, PRINTS, "postgresql://test", "redis://test")


if __name__ == "__main__":
    unittest.main()
