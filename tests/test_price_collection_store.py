"""Real database checks for the global collection interval; no shop HTTP."""
import hashlib
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import psycopg
from psycopg import sql

import price_collector
import price_store
from test_price_collector import NUMBER, search_html


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "requires disposable PostgreSQL database")
class CollectionStoreTest(unittest.TestCase):
    def setUp(self):
        self.db = os.environ["TEST_DATABASE_URL"]
        if not urlsplit(self.db).path.lstrip("/").startswith("ygosolver_test_"):
            raise RuntimeError("refusing to modify a non-test database")
        price_store.init_db(self.db)
        with psycopg.connect(self.db) as conn:
            for (table,) in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall():
                conn.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(sql.Identifier(table)))
        price_store.init_db(self.db)
        self.url = price_collector.search_url(NUMBER, "ja")

    def reserve(self):
        return price_store.reserve_collection(NUMBER, "ja", self.url, self.db)

    def capture(self, observed=None):
        html = search_html()
        raw = html.encode("euc-kr")
        metadata = {"url": self.url, "observed_at": (observed or datetime.now(timezone.utc)).isoformat(),
                    "status_code": 200, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                    "content_type": "text/html"}
        products = price_collector.parse_search_page(html, NUMBER, "ja")
        return metadata, products

    def test_concurrent_reservations_allow_exactly_one_request(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.reserve(), range(6)))
        allowed = [r for r in results if r["allowed"]]
        self.assertEqual(len(allowed), 1)
        self.assertEqual(allowed[0]["next_allowed_at"] - allowed[0]["reserved_at"], timedelta(hours=12))
        with psycopg.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM price_collection_attempts").fetchone()[0], 1)

    def test_failure_and_reinitialization_do_not_reset_interval(self):
        permit = self.reserve()
        price_store.finish_attempt(permit["attempt_id"], "HTTP 429 proof", self.db)
        price_store.init_db(self.db)
        result = self.reserve()
        self.assertFalse(result["allowed"])
        self.assertEqual(result["next_allowed_at"], permit["next_allowed_at"])
        last = price_store.latest_attempt(NUMBER, "ja", self.db)
        self.assertEqual((last["outcome"], last["error"]), ("failed", "HTTP 429 proof"))

    def test_migration_is_seeded_from_existing_recent_snapshot(self):
        observed = datetime.now(timezone.utc) - timedelta(hours=3)
        metadata, products = self.capture(observed)
        price_store.store_listing(metadata, products, self.db)
        with psycopg.connect(self.db) as conn:
            conn.execute("DELETE FROM price_collection_gate")
        price_store.init_db(self.db)
        result = self.reserve()
        self.assertFalse(result["allowed"])
        self.assertEqual(result["next_allowed_at"], observed + timedelta(hours=12))

    def test_new_import_after_initialization_also_prevents_immediate_request(self):
        observed = datetime.now(timezone.utc) - timedelta(minutes=5)
        price_store.store_listing(*self.capture(observed), self.db)
        result = self.reserve()
        self.assertFalse(result["allowed"])
        self.assertEqual(result["next_allowed_at"], observed + timedelta(hours=12))

    def test_snapshot_and_attempt_commit_together_and_bad_data_rolls_back(self):
        permit = self.reserve()
        metadata, products = self.capture()
        products[0]["price_krw"] = -1
        with self.assertRaises(price_store.PriceError):
            price_store.store_listing(metadata, products, self.db, attempt_id=permit["attempt_id"])
        with psycopg.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM price_snapshots").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT outcome FROM price_collection_attempts").fetchone()[0], "pending")
        metadata, products = self.capture()
        saved = price_store.store_listing(metadata, products, self.db, attempt_id=permit["attempt_id"])
        attempt = price_store.latest_attempt(NUMBER, "ja", self.db)
        self.assertEqual(attempt["outcome"], "stored")
        self.assertEqual(attempt["snapshot_id"], saved["snapshot_id"])
        self.assertEqual(attempt["product_count"], 1)
        self.assertFalse(self.reserve()["allowed"])


if __name__ == "__main__":
    unittest.main()
