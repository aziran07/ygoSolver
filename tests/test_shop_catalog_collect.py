"""Collector acceptance: fake retailer HTTP, real isolated PostgreSQL persistence."""

import gzip
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import requests

DATABASE = os.environ.get("TEST_DATABASE_URL")
FIXTURES = Path(__file__).parent / "fixtures" / "shop_catalog"


def body(name):
    return gzip.decompress((FIXTURES / f"{name}.html.gz").read_bytes())


class FakeShop:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected extra retailer request: {url}")
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        status, content, content_type = value
        response = requests.Response()
        response.status_code = status
        response._content = content
        response.url = url
        response.headers["Content-Type"] = content_type
        if status == 302:
            response.headers["Location"] = "http://example.invalid/login"
        return response


@unittest.skipUnless(DATABASE, "requires disposable PostgreSQL TEST_DATABASE_URL")
class CatalogCollectorContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not urlsplit(DATABASE).path.lstrip("/").startswith("ygosolver_test_"):
            raise RuntimeError("collector tests require ygosolver_test_ database")
        import psycopg
        import shop_catalog_collect
        import shop_catalog_store
        cls.pg = psycopg
        cls.collector = shop_catalog_collect
        cls.store = shop_catalog_store
        cls.store.init_db(DATABASE)

    def setUp(self):
        self.run_ids = []

    def start(self, category="101015"):
        run = self.store.start_run(DATABASE, "cardmoa", category, "ko",
                                   f"http://cardmoa.com/shop/list.php?ca_id={category}")
        self.run_ids.append(run)
        return run

    def collect(self, session, category="101015", **kwargs):
        return self.collector.collect_catalog(DATABASE, "cardmoa", category, session=session, **kwargs)

    def test_complete_single_page_saves_every_price_and_original_response(self):
        source = body("cardmoa_ko_condition")
        shop = FakeShop((200, source, "text/html; charset=euc-kr"))
        result = self.collect(shop)
        self.assertEqual(result["status"], "completed")
        self.assertEqual((result["page_count"], result["product_count"]), (1, 16))
        self.assertEqual(len(shop.calls), 1)
        self.assertIs(shop.calls[0][1]["allow_redirects"], False)
        self.assertEqual(shop.calls[0][1]["timeout"], (5, 20))
        with self.pg.connect(DATABASE) as connection:
            saved = connection.execute("SELECT body_gzip FROM shop_catalog_pages WHERE run_id=%s",
                                       (result["id"],)).fetchone()[0]
        self.assertEqual(gzip.decompress(saved), source)

    def test_max_pages_cannot_report_full_catalog_and_resume_starts_at_next_page(self):
        first = FakeShop((200, body("cardmoa_ko_first"), "text/html"))
        result = self.collect(first, "101010", max_pages=1)
        self.assertEqual(result["status"], "running")
        self.assertEqual((result["page_count"], result["product_count"]), (1, 140))
        next_page = FakeShop((429, b"rate limit", "text/html"))
        with self.assertRaises(self.collector.CatalogCollectionError):
            self.collect(next_page, "101010", run_id=result["id"], max_pages=1)
        self.assertEqual(len(next_page.calls), 1)
        self.assertIn("page=2", next_page.calls[0][0])
        after = self.store.get_run(DATABASE, result["id"])
        self.assertEqual(after["status"], "failed")
        self.assertEqual(after["stored_pages"], [1])
        self.assertIn("429", after["error"])

    def test_http_denials_redirects_and_wrong_content_never_retry_or_complete(self):
        for status, payload, content_type in (
            (403, b"denied", "text/html"), (429, b"limited", "text/html"),
            (302, b"redirect", "text/html"), (500, b"server error", "text/html"),
            (200, b"{}", "application/json"), (200, b"", "text/html"),
            (200, b"<html>Error</html>", "text/html"),
        ):
            with self.subTest(status=status, content_type=content_type, payload=payload):
                run = self.start()
                shop = FakeShop((status, payload, content_type))
                with self.assertRaises(self.collector.CatalogCollectionError):
                    self.collect(shop, run_id=run)
                self.assertEqual(len(shop.calls), 1)
                state = self.store.get_run(DATABASE, run)
                self.assertEqual(state["status"], "failed")
                self.assertEqual(state["page_count"], 0)
                self.assertEqual(state["failure_count"], 1)
                self.assertTrue(state["error"])

    def test_network_exception_is_recorded_with_cause(self):
        run = self.start()
        shop = FakeShop(requests.Timeout("test connect timeout"))
        with self.assertRaises(self.collector.CatalogCollectionError):
            self.collect(shop, run_id=run)
        state = self.store.get_run(DATABASE, run)
        self.assertEqual(state["status"], "failed")
        self.assertIn("test connect timeout", state["error"])
        self.assertEqual(len(shop.calls), 1)

    def test_resume_rejects_wrong_category_without_contacting_shop(self):
        run = self.start("101010")
        shop = FakeShop()
        with self.assertRaises((self.collector.CatalogCollectionError, self.store.ShopCatalogError, ValueError)):
            self.collect(shop, "101015", run_id=run)
        self.assertEqual(shop.calls, [])

    def test_failed_completion_is_resumable_without_losing_failure_history(self):
        run = self.start()
        shop = FakeShop((200, body("cardmoa_ko_condition"), "text/html"))
        with patch.object(self.store, "complete_run", side_effect=self.store.ShopCatalogError("test finalization interruption")):
            with self.assertRaises(self.collector.CatalogCollectionError):
                self.collect(shop, run_id=run)
        self.assertEqual(self.store.get_run(DATABASE, run)["status"], "failed")
        self.assertEqual(self.store.get_run(DATABASE, run)["product_count"], 16)
        result = self.collect(FakeShop(), run_id=run)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["failure_count"], 1)

    def test_invalid_collection_limits_fail_before_request(self):
        for kwargs in ({"max_pages": 0}, {"max_pages": -1}, {"delay": 0}, {"delay": float("nan")}):
            with self.subTest(kwargs=kwargs):
                shop = FakeShop()
                with self.assertRaises((ValueError, self.collector.CatalogCollectionError)):
                    self.collect(shop, **kwargs)
                self.assertEqual(shop.calls, [])

    def test_resume_setup_failure_is_recorded_once_before_any_http(self):
        first = FakeShop((200, body("cardmoa_ko_first"), "text/html"))
        result = self.collect(first, "101010", max_pages=1)
        shop = FakeShop()
        with patch.object(self.store, "page_product_count", side_effect=self.store.ShopCatalogError("stored page unavailable")):
            with self.assertRaises(self.collector.CatalogCollectionError) as caught:
                self.collect(shop, "101010", run_id=result["id"])
        self.assertIn("stored page unavailable", str(caught.exception))
        state = self.store.get_run(DATABASE, result["id"])
        self.assertEqual((state["status"], state["failure_count"]), ("failed", 1))
        self.assertEqual(state["stored_pages"], [1])
        self.assertEqual(shop.calls, [])
