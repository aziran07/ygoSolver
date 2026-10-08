"""Real DB checks for immediate targeted collection and durable attempts; no shop HTTP."""
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

    def begin(self):
        return price_store.begin_collection(NUMBER, "ja", self.url, self.db)

    def capture(self, observed=None):
        html = search_html()
        raw = html.encode("euc-kr")
        metadata = {"url": self.url, "observed_at": (observed or datetime.now(timezone.utc)).isoformat(),
                    "status_code": 200, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                    "content_type": "text/html"}
        products = price_collector.parse_search_page(html, NUMBER, "ja")
        return metadata, products

    def test_attempts_have_unique_durable_ids_without_a_global_cooldown(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.begin(), range(6)))
        self.assertEqual(len({r['attempt_id'] for r in results}), 6)
        self.assertTrue(all(r['started_at'].tzinfo is not None for r in results))
        with psycopg.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM price_collection_attempts").fetchone()[0], 6)

    def test_failed_attempt_remains_visible_and_does_not_block_a_new_explicit_attempt(self):
        permit = self.begin()
        price_store.finish_attempt(permit["attempt_id"], "HTTP 429 proof", self.db)
        price_store.init_db(self.db)
        last = price_store.latest_attempt(NUMBER, "ja", self.db)
        self.assertEqual((last["outcome"], last["error"]), ("failed", "HTTP 429 proof"))
        result = self.begin()
        self.assertNotEqual(result['attempt_id'], permit['attempt_id'])

    def test_old_future_global_gate_is_ignored_and_preserved(self):
        future = datetime.now(timezone.utc) + timedelta(days=3)
        with psycopg.connect(self.db) as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS price_collection_gate (
                name TEXT PRIMARY KEY, last_request_at TIMESTAMPTZ, next_allowed_at TIMESTAMPTZ NOT NULL,
                seeded_from TEXT NOT NULL)''')
            conn.execute('''INSERT INTO price_collection_gate (name,next_allowed_at,seeded_from)
                VALUES ('tcgshop',%s,'legacy test') ON CONFLICT(name) DO UPDATE SET next_allowed_at=excluded.next_allowed_at''',
                (future,))
        price_store.init_db(self.db)
        self.assertGreater(self.begin()['attempt_id'], 0)
        with psycopg.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT next_allowed_at FROM price_collection_gate WHERE name='tcgshop'").fetchone()[0], future)

    def test_recent_snapshot_does_not_block_collection_for_another_card(self):
        observed = datetime.now(timezone.utc) - timedelta(minutes=5)
        price_store.store_listing(*self.capture(observed), self.db)
        result = price_store.begin_collection('RV01-JP069', 'ja',
                                              price_collector.search_url('RV01-JP069', 'ja'), self.db)
        self.assertGreater(result['attempt_id'], 0)

    def test_snapshot_and_attempt_commit_together_and_bad_data_rolls_back(self):
        permit = self.begin()
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
        self.assertNotEqual(self.begin()['attempt_id'], permit['attempt_id'])

    def test_legacy_attempt_migration_preserves_failure_and_supports_verified_empty(self):
        with psycopg.connect(self.db) as conn:
            conn.execute('ALTER TABLE price_collection_attempts DROP CONSTRAINT price_collection_attempts_outcome_check')
            conn.execute('''ALTER TABLE price_collection_attempts ADD CONSTRAINT price_collection_attempts_outcome_check
                CHECK(outcome IN ('pending','stored','failed'))''')
        failed = self.begin()
        price_store.finish_attempt(failed['attempt_id'], 'old failure evidence', self.db)
        price_store.init_db(self.db)
        price_store.init_db(self.db)
        previous = price_store.latest_attempt(NUMBER, 'ja', self.db)
        self.assertEqual((previous['id'], previous['error']), (failed['attempt_id'], 'old failure evidence'))
        empty = self.begin()
        finished = price_store.record_empty(empty['attempt_id'], self.db)
        latest = price_store.latest_attempt(NUMBER, 'ja', self.db)
        self.assertEqual((latest['outcome'], latest['product_count'], latest['finished_at']), ('empty', 0, finished))
        self.assertIsNone(latest['snapshot_id'])
        with psycopg.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM price_snapshots').fetchone()[0], 0)
        with self.assertRaises(price_store.PriceError):
            price_store.record_empty(empty['attempt_id'], self.db)

    def test_empty_attempt_cannot_claim_products(self):
        attempt = self.begin()
        with self.assertRaises(psycopg.errors.CheckViolation), psycopg.connect(self.db) as conn:
            conn.execute('''UPDATE price_collection_attempts SET outcome='empty',finished_at=now(),product_count=1
                WHERE id=%s''', (attempt['attempt_id'],))


if __name__ == "__main__":
    unittest.main()
