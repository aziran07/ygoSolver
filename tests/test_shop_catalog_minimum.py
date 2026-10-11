"""Minimum-price behavior, tested against disposable PostgreSQL only."""

from datetime import datetime, timedelta, timezone
import os
import unittest
from urllib.parse import urlsplit
from uuid import uuid4

DATABASE = os.environ.get("TEST_DATABASE_URL")


@unittest.skipUnless(DATABASE, "requires disposable PostgreSQL TEST_DATABASE_URL")
class CatalogMinimumContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not urlsplit(DATABASE).path.lstrip("/").startswith("ygosolver_test_"):
            raise RuntimeError("minimum tests require a disposable ygosolver_test_ database")
        import psycopg
        import shop_catalog_store
        cls.pg = psycopg
        cls.store = shop_catalog_store
        cls.store.init_db(DATABASE)

    def setUp(self):
        self.code = "T" + uuid4().hex[:8].upper() + "-KR001"
        self.category = "test-" + uuid4().hex
        self.runs = []

    def row(self, product_id, price, shop="tcgshop", **changes):
        base = "http://www.tcgshop.co.kr/goods_detail.php?goodsIdx=" if shop == "tcgshop" else "http://cardmoa.com/shop/item.php?it_id="
        raw = {"locale_evidence": "direct printed KR code", "list_price_text": None,
               "condition_text": "A급"}
        if price is None:
            raw["price_error"] = "no numeric sale price displayed"
        result = dict(product_id=product_id, name="Source name is not an identity key",
                      product_url=base + product_id, locale="ko", card_number=self.code,
                      rarity_label="Ultra Rare" if shop == "tcgshop" else "울트라레어",
                      price_krw=price, list_price_krw=None, stock_status="in_stock", raw=raw)
        result.update(changes)
        return result

    def save_run(self, shop, rows, complete=True):
        url = "http://www.tcgshop.co.kr/goods_list.php?Index=276" if shop == "tcgshop" else "http://cardmoa.com/shop/list.php?ca_id=101010"
        run = self.store.start_run(DATABASE, shop, self.category, "ko", url)
        self.runs.append(run)
        self.store.store_page(DATABASE, run, 1, url, datetime.now(timezone.utc) - timedelta(seconds=1),
                              b"independent minimum-price evidence " + str(run).encode(),
                              dict(products=rows, total_products=len(rows), total_pages=1))
        if complete:
            self.store.complete_run(DATABASE, run)
        return run

    def minima(self):
        with self.pg.connect(DATABASE, row_factory=self.pg.rows.dict_row) as connection:
            return connection.execute(
                "SELECT * FROM shop_catalog_lowest_prices WHERE run_id = ANY(%s)", (self.runs,)
            ).fetchall()

    def test_condition_and_stock_do_not_override_lower_cross_shop_price(self):
        self.save_run("tcgshop", [self.row("1", 900)])
        source_run = self.save_run("cardmoa", [self.row("2", 300, "cardmoa", card_number=self.code + "A급",
                                                      stock_status="out_of_stock")])
        rows = self.minima()
        self.assertEqual(len(rows), 1)
        result = rows[0]
        self.assertEqual((result["price_krw"], result["shop"], result["product_id"]), (300, "cardmoa", "2"))
        self.assertEqual((result["run_id"], result["page_number"], result["card_number"]), (source_run, 1, self.code))
        self.assertIn("it_id=2", result["product_url"])
        self.assertIsNotNone(result["observed_at"])

    def test_edition_finish_and_art_markers_remain_separate(self):
        self.save_run("tcgshop", [
            self.row("1", 900),
            self.row("2", 300, rarity_label="UR OverFrame"),
            self.row("3", 200, locale="ja", card_number=self.code.replace("-KR", "-JP")),
            self.row("4", 100, card_number=self.code + "(신)"),
            self.row("5", 700, rarity_label="PSC OverFrame"),
            self.row("6", 600, rarity_label="Prismatic Secret Rare"),
        ])
        self.assertEqual({row["product_id"] for row in self.minima()}, {"1", "2", "3", "4", "5", "6"})

    def test_unpriced_and_unresolved_items_do_not_become_zero_or_merge(self):
        self.save_run("tcgshop", [self.row("1", 900), self.row("2", None),
                                  self.row("3", 100, locale=None),
                                  {k: v for k, v in self.row("4", 200).items() if k != "rarity_label"},
                                  {k: v for k, v in self.row("5", 300).items() if k != "card_number"}])
        self.assertEqual([(row["product_id"], row["price_krw"]) for row in self.minima()], [("1", 900)])

    def test_newer_failed_or_running_run_never_reuses_older_completed_prices(self):
        self.save_run("tcgshop", [self.row("1", 50)])
        latest = self.save_run("tcgshop", [self.row("2", 900)], complete=False)
        self.assertEqual(self.minima(), [])
        self.store.fail_run(DATABASE, latest, "page validation failure")
        self.assertEqual(self.minima(), [])
        self.store.reopen_run(DATABASE, latest)
        self.store.complete_run(DATABASE, latest)
        self.assertEqual([(row["product_id"], row["price_krw"]) for row in self.minima()], [("2", 900)])

    def test_ambiguous_shop_rarity_cannot_collapse_different_finishes(self):
        self.save_run("tcgshop", [self.row("1", 900, rarity_label="Secret Rare", name="Card"),
                                  self.row("2", 300, rarity_label="Secret Rare", name="Card_SpecialRedVer.")])
        rows = self.minima()
        self.assertEqual({row["product_id"] for row in rows}, {"1", "2"})
        self.assertTrue(all(row["rarity_mapping_status"] == "unverified_finish" for row in rows))
