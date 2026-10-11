"""Independent catalog persistence contracts; never use the production database."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import os
import unittest
from urllib.parse import urlsplit
from uuid import uuid4


DATABASE = os.environ.get("TEST_DATABASE_URL")
TCG_URL = "http://www.tcgshop.co.kr/goods_list.php?Index=276"
CM_URL = "http://cardmoa.com/shop/list.php?ca_id=101010"


def product(product_id="001", shop="tcgshop", **changes):
    url = (f"http://www.tcgshop.co.kr/goods_detail.php?goodsIdx={product_id}"
           if shop == "tcgshop" else f"http://cardmoa.com/shop/item.php?it_id={product_id}")
    row = dict(product_id=product_id, name="검증용 카드", locale="ko", product_url=url,
               price_krw=240, list_price_krw=300, stock_status="out_of_stock",
               card_number="DP23-KR020", rarity_label="Normal",
               raw={"price_text": "240원", "list_price_text": "300원",
                    "locale_evidence": "Korean OCG category", "stock_evidence": "품절"})
    row.update(changes)
    return row


@unittest.skipUnless(DATABASE, "requires disposable PostgreSQL TEST_DATABASE_URL")
class CatalogStoreContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not urlsplit(DATABASE).path.lstrip("/").startswith("ygosolver_test_"):
            raise RuntimeError("TEST_DATABASE_URL must name a ygosolver_test_ database")
        import shop_catalog_store
        import psycopg
        cls.store = shop_catalog_store
        cls.pg = psycopg
        cls.store.init_db(DATABASE)

    def setUp(self):
        self.observed = datetime.now(timezone.utc) - timedelta(seconds=10)
        self.run = self.start()

    def start(self, shop="tcgshop", locale="ko"):
        return self.store.start_run(DATABASE, shop, "test-" + uuid4().hex, locale,
                                    TCG_URL if shop == "tcgshop" else CM_URL)

    def save(self, number=1, rows=None, total=2, pages=2, run=None, url=TCG_URL,
             body=b"source page one", observed=None):
        parsed = {"products": [product()] if rows is None else rows,
                  "total_products": total, "total_pages": pages}
        return self.store.store_page(DATABASE, self.run if run is None else run,
                                     number, url, observed or self.observed, body, parsed)

    def state(self, run=None):
        return self.store.get_run(DATABASE, self.run if run is None else run)

    def test_full_run_requires_all_pages_and_exact_product_count(self):
        self.assertEqual(self.save(), 1)
        with self.assertRaises(self.store.ShopCatalogError):
            self.store.complete_run(DATABASE, self.run)
        self.assertNotEqual(self.state()["status"], "completed")
        self.save(2, [product("002")], body=b"source page two")
        self.store.complete_run(DATABASE, self.run)
        result = self.state()
        self.assertEqual(result["status"], "completed")
        self.assertEqual((result["page_count"], result["product_count"]), (2, 2))
        self.assertEqual(result["stored_pages"], [1, 2])

    def test_declared_count_cannot_hide_missing_or_extra_products(self):
        for rows, total in (([product()], 2), ([product(), product("002")], 1)):
            with self.subTest(total=total, actual=len(rows)):
                run = self.start()
                try:
                    self.save(rows=rows, total=total, pages=1, run=run)
                    self.store.complete_run(DATABASE, run)
                except self.store.ShopCatalogError:
                    pass
                self.assertNotEqual(self.state(run)["status"], "completed")

    def test_repeated_page_is_idempotent_but_changed_evidence_is_rejected(self):
        self.save()
        self.assertEqual(self.save(), 0)
        self.assertEqual(self.state()["product_count"], 1)
        for kwargs in ({"body": b"changed source"}, {"rows": [product(price_krw=999)]},
                       {"observed": self.observed + timedelta(seconds=1)}):
            with self.subTest(kwargs=kwargs), self.assertRaises(self.store.ShopCatalogError):
                self.save(**kwargs)
        self.assertEqual(self.state()["page_count"], 1)

    def test_duplicate_product_across_pages_rolls_back_whole_page(self):
        self.save(total=3)
        with self.assertRaises(self.store.ShopCatalogError):
            self.save(2, [product("002"), product()], total=3)
        self.assertEqual(self.state()["stored_pages"], [1])
        self.assertEqual(self.state()["product_count"], 1)

    def test_duplicate_on_same_page_is_not_silently_deduplicated(self):
        with self.assertRaises(self.store.ShopCatalogError):
            self.save(rows=[product(), product()])
        self.assertEqual(self.state()["product_count"], 0)

    def test_changed_catalog_totals_do_not_create_a_complete_run(self):
        self.save()
        for total, pages in ((3, 2), (2, 3)):
            with self.subTest(total=total, pages=pages), self.assertRaises(self.store.ShopCatalogError):
                self.save(2, [product("002")], total=total, pages=pages)
        self.assertEqual(self.state()["stored_pages"], [1])

    def test_failure_preserves_pages_and_explicit_resume_can_complete(self):
        self.save()
        self.store.fail_run(DATABASE, self.run, "page 2: HTTP 429")
        result = self.state()
        self.assertEqual(result["status"], "failed")
        self.assertIn("HTTP 429", result["error"])
        self.assertEqual(result["stored_pages"], [1])
        self.save(2, [product("002")], body=b"page two after explicit resume")
        self.store.complete_run(DATABASE, self.run)
        self.assertEqual(self.state()["status"], "completed")
        self.assertEqual(self.state()["failure_count"], 1)
        with self.pg.connect(DATABASE) as connection:
            failure = connection.execute(
                "SELECT error FROM shop_catalog_failures WHERE run_id = %s", (self.run,)
            ).fetchone()
        self.assertEqual(failure, ("page 2: HTTP 429",))

    def test_completed_run_cannot_be_overwritten_or_marked_failed(self):
        self.save(total=1, pages=1)
        self.store.complete_run(DATABASE, self.run)
        with self.assertRaises(self.store.ShopCatalogError):
            self.save(rows=[product(price_krw=999)], total=1, pages=1)
        with self.assertRaises(self.store.ShopCatalogError):
            self.store.fail_run(DATABASE, self.run, "late failure")
        self.assertEqual(self.state()["status"], "completed")

    def test_same_product_id_in_two_shops_and_later_runs_does_not_collide(self):
        other_shop = self.start("cardmoa")
        later = self.start()
        self.save(total=1, pages=1)
        self.save(rows=[product(shop="cardmoa", price_krw=900)], run=other_shop,
                  url=CM_URL, total=1, pages=1)
        self.save(rows=[product(price_krw=600)], run=later, total=1, pages=1)
        for run in (self.run, other_shop, later):
            self.store.complete_run(DATABASE, run)
            self.assertEqual(self.state(run)["product_count"], 1)

    def test_unlisted_list_price_is_null_and_unreadable_sale_price_has_reason(self):
        row = product(list_price_krw=None)
        row["raw"]["list_price_text"] = None
        unreadable = product("002", price_krw=None)
        unreadable["raw"].update(price_text="문의", price_error="shop displays inquiry, not a numeric price")
        self.save(rows=[row, unreadable], total=2, pages=1)
        self.store.complete_run(DATABASE, self.run)
        self.assertEqual(self.state()["product_count"], 2)

    def test_invalid_values_fail_before_page_commit(self):
        changes = [dict(price_krw=-1), dict(price_krw=True), dict(price_krw=1.5),
                   dict(price_krw=None), dict(price_krw=0), dict(stock_status="sold"),
                   dict(product_id=1), dict(product_id=""), dict(locale="en"),
                   dict(product_url="http://evil.example/goods_detail.php?goodsIdx=1")]
        for changed in changes:
            with self.subTest(changed=changed), self.assertRaises(self.store.ShopCatalogError):
                self.save(rows=[product(**changed)], total=1, pages=1)
        self.assertEqual(self.state()["page_count"], 0)

    def test_url_and_time_evidence_cannot_be_forged(self):
        for changed in ({"url": CM_URL}, {"url": "http://www.tcgshop.co.kr.evil.example/"},
                        {"url": "http://user:password@www.tcgshop.co.kr/"},
                        {"observed": datetime.now()},
                        {"observed": datetime.now(timezone.utc) + timedelta(days=1)},
                        {"body": b""}):
            with self.subTest(changed=changed), self.assertRaises(self.store.ShopCatalogError):
                self.save(**changed)
        self.assertEqual(self.state()["page_count"], 0)

    def test_empty_catalog_needs_one_observed_page(self):
        with self.assertRaises(self.store.ShopCatalogError):
            self.store.complete_run(DATABASE, self.run)
        self.save(rows=[], total=0, pages=1, body=b"confirmed zero-product catalog")
        self.store.complete_run(DATABASE, self.run)
        self.assertEqual(self.state()["status"], "completed")
        self.assertEqual(self.state()["product_count"], 0)

    def test_out_of_range_page_is_not_saved(self):
        for number in (0, -1, 3, True):
            with self.subTest(number=number), self.assertRaises(self.store.ShopCatalogError):
                self.save(number)
        self.assertEqual(self.state()["page_count"], 0)

    def test_schema_init_preserves_existing_completed_history(self):
        self.save(total=1, pages=1)
        self.store.complete_run(DATABASE, self.run)
        before = deepcopy(self.state())
        self.store.init_db(DATABASE)
        self.assertEqual(self.state(), before)

    def test_original_bytes_and_unmapped_product_evidence_survive_round_trip(self):
        body = b"<html>unusual source bytes: \xff\x00\xec\xa0\x84</html>"
        row = product()
        row["raw"]["condition_label"] = "A급"
        self.save(rows=[row], total=1, pages=1, body=body)
        with self.pg.connect(DATABASE) as connection:
            saved = connection.execute(
                "SELECT body_gzip, sha256, bytes, observed_at FROM shop_catalog_pages "
                "WHERE run_id = %s", (self.run,)
            ).fetchone()
            saved_product = connection.execute(
                "SELECT product_id, price_krw, list_price_krw, stock_status, product "
                "FROM shop_catalog_products WHERE run_id = %s", (self.run,)
            ).fetchone()
        self.assertEqual(gzip.decompress(saved[0]), body)
        self.assertEqual(saved[1:], (hashlib.sha256(body).hexdigest(), len(body), self.observed))
        self.assertEqual(saved_product, ("001", 240, 300, "out_of_stock", row))
