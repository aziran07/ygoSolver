"""Real PostgreSQL/Redis acceptance checks. Use a disposable test DB and Redis DB 15.

TEST_DATABASE_URL must name a database prefixed ygosolver_test_; never production.
No retailer network requests are made. Fixture timestamps below are synthetic.
"""
import hashlib
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch
from urllib.parse import urlsplit

from bs4 import BeautifulSoup


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL") and os.environ.get("TEST_REDIS_URL"),
                     "requires disposable PostgreSQL and Redis integration databases")
class PriceStoreIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        import redis
        import price_store
        cls.pg = psycopg
        cls.store = price_store
        cls.database_url = os.environ["TEST_DATABASE_URL"]
        cls.redis_url = os.environ["TEST_REDIS_URL"]
        if not urlsplit(cls.database_url).path.lstrip("/").startswith("ygosolver_test_"):
            raise RuntimeError("test database name must start with ygosolver_test_")
        if urlsplit(cls.redis_url).path != "/15":
            raise RuntimeError("integration tests require dedicated Redis DB 15")
        cls.cache = redis.Redis.from_url(cls.redis_url, decode_responses=True)
        cls.store.init_db(cls.database_url)

    def setUp(self):
        from psycopg import sql
        with self.pg.connect(self.database_url) as connection:
            tables = connection.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall()
            for (table,) in tables:
                connection.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(sql.Identifier(table)))
        self.cache.flushdb()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.observed = datetime.now(timezone.utc) - timedelta(hours=3)

    def capture(self, observed=None, prices=None, only=None, soldout=None, korean=False):
        fixture = Path(__file__).parent / "fixtures" / "tcgshop_ja_list.html"
        soup = BeautifulSoup(fixture.read_text(encoding="utf-8"), "html.parser")
        for block in list(soup.select('table[id^="list_card_"]')):
            product_id = block["id"].removeprefix("list_card_")
            if only is not None and product_id not in only:
                block.decompose()
                continue
            if prices and product_id in prices:
                block.select_one(".glist_price12").string = f"{prices[product_id]:,}"
            if product_id == soldout:
                cart = next(image for image in block.select("img") if image.get("src", "").endswith("go_cart.gif"))
                cart["src"] = "dis_go_cart.gif"
                cart.attrs.pop("onclick", None)
                block.append("품절")
        text = str(soup)
        index = "288"
        if korean:
            soup.select_one('input[name="Index"]')["value"] = "276"
            text = str(soup).replace("-JP", "-KR")
            index = "276"
        raw = text.encode("euc-kr")
        html = self.directory / "snapshot.bin"
        metadata = self.directory / "snapshot.json"
        html.write_bytes(raw)
        metadata.write_text(json.dumps({
            "url": f"http://www.tcgshop.co.kr/goods_list.php?Index={index}",
            "observed_at": (observed or self.observed).isoformat(),
            "status_code": 200, "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw), "content_type": "text/html",
        }), encoding="utf-8")
        return html, metadata

    def ingest(self, **kwargs):
        html, metadata = self.capture(**kwargs)
        return self.store.import_snapshot(html, metadata, self.database_url, self.redis_url)

    def lookup(self, card_number="YAC1-JP002", locale="ja", **kwargs):
        return self.store.get_prices(card_number, locale,
                                    kwargs.get("database_url", self.database_url),
                                    kwargs.get("redis_url", self.redis_url))

    def only_cache_key(self):
        keys = list(self.cache.scan_iter())
        self.assertEqual(len(keys), 1)
        return keys[0]

    def test_variants_stock_provenance_and_shared_hit(self):
        self.ingest(soldout="134043")
        first = self.lookup()
        self.assertEqual(first["cache_status"], "miss")
        self.assertEqual(first["card_number"], "YAC1-JP002")
        self.assertEqual(first["locale"], "ja")
        self.assertEqual(first["scope"], "observed_products")
        products = {row["product_id"]: row for row in first["prices"]}
        self.assertEqual(set(products), {"134053", "134043"})
        self.assertEqual((products["134053"]["rarity_label"], products["134053"]["price_krw"], products["134053"]["stock_status"]),
                         ("PSC OverFrame", 28000, "in_stock"))
        self.assertEqual((products["134043"]["rarity_label"], products["134043"]["price_krw"], products["134043"]["stock_status"]),
                         ("UR OverFrame", 6000, "out_of_stock"))
        for row in products.values():
            self.assertEqual(datetime.fromisoformat(row["observed_at"]), self.observed)
            self.assertTrue(row["stock_evidence"])
            self.assertIn(f"goodsIdx={row['product_id']}", row["product_url"])
        second = self.lookup()
        self.assertEqual(second["cache_status"], "hit")
        self.assertEqual(second["prices"], first["prices"])

    def test_cache_delete_repopulates_only_remaining_lifetime(self):
        self.ingest()
        first = self.lookup()
        key = self.only_cache_key()
        expected_expiry = self.observed + timedelta(hours=12)
        self.assertEqual(datetime.fromisoformat(first["expires_at"]), expected_expiry)
        for _ in range(2):
            ttl = self.cache.pttl(key)
            remaining = (expected_expiry - datetime.now(timezone.utc)).total_seconds() * 1000
            self.assertLessEqual(abs(ttl - remaining), 1500)
            self.assertLess(ttl, 10 * 3600 * 1000)
            self.cache.delete(key)
            repopulated = self.lookup()
            self.assertEqual(repopulated["cache_status"], "miss")
            self.assertEqual(repopulated["expires_at"], first["expires_at"])

    def test_exact_language_and_number_do_not_mix(self):
        self.ingest()
        self.ingest(korean=True, prices={"134043": 800})
        japanese = self.lookup()
        korean = self.lookup("YAC1-KR002", "ko")
        self.assertEqual({r["price_krw"] for r in japanese["prices"]}, {6000, 28000})
        self.assertEqual({r["price_krw"] for r in korean["prices"]}, {800, 28000})
        self.assertTrue(all(row["locale"] == "ko" for row in korean["prices"]))
        with self.assertRaises(self.store.PriceError):
            self.lookup("YAC1-JP002", "ko")
        with self.assertRaises(self.store.PriceError):
            self.lookup(locale="en")

    def test_missing_is_explicit_and_not_negative_cached(self):
        with self.assertRaises(self.store.PriceNotFound):
            self.lookup()
        self.assertEqual(self.cache.dbsize(), 0)

    def test_expired_is_not_returned_or_cached(self):
        self.ingest(observed=datetime.now(timezone.utc) - timedelta(hours=12, seconds=1))
        with self.assertRaises(self.store.StalePriceError):
            self.lookup()
        self.assertEqual(self.cache.dbsize(), 0)

    def test_price_expiring_during_database_read_is_not_returned(self):
        start = datetime.now(timezone.utc)
        self.ingest(observed=start - timedelta(hours=12) + timedelta(seconds=1))

        class Clock(datetime):
            current = start

            @classmethod
            def now(cls, tz=None):
                return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)

        real_query = self.store.query_latest_products

        def slow_query(*args):
            rows = real_query(*args)
            Clock.current = start + timedelta(seconds=2)
            return rows

        with patch.object(self.store, "datetime", Clock), patch.object(self.store, "query_latest_products", side_effect=slow_query):
            with self.assertRaises(self.store.StalePriceError):
                self.lookup()
        self.assertEqual(self.cache.dbsize(), 0)

    def test_partly_stale_variants_are_not_silently_omitted(self):
        self.ingest(observed=self.observed - timedelta(hours=12))
        self.ingest(only={"134053"})
        with self.assertRaises(self.store.StalePriceError):
            self.lookup()

    def test_import_idempotency_and_conflicting_snapshot_rollback(self):
        self.ingest()
        self.ingest()
        self.assertEqual(len(self.lookup()["prices"]), 2)
        with self.assertRaises(self.store.PriceError):
            self.ingest(prices={"134043": 7000})
        self.assertEqual({r["price_krw"] for r in self.lookup()["prices"]}, {6000, 28000})

    def test_new_observation_does_not_reuse_old_cache(self):
        self.ingest()
        self.lookup()
        self.ingest(observed=self.observed + timedelta(minutes=1), prices={"134043": 9000})
        changed = self.lookup()
        self.assertEqual(changed["cache_status"], "miss")
        self.assertEqual({r["price_krw"] for r in changed["prices"]}, {9000, 28000})
        self.assertEqual(self.lookup()["cache_status"], "hit")

    def test_late_arriving_older_capture_cannot_replace_newer_price(self):
        self.ingest()
        self.lookup()
        self.ingest(observed=self.observed - timedelta(hours=1), prices={"134043": 1000})
        latest = self.lookup()
        self.assertEqual({r["price_krw"] for r in latest["prices"]}, {6000, 28000})
        self.assertEqual(datetime.fromisoformat(latest["expires_at"]), self.observed + timedelta(hours=12))

    def test_bad_capture_never_changes_committed_prices(self):
        self.ingest()
        html, metadata = self.capture(prices={"134043": 50})
        html.write_bytes(html.read_bytes() + b"tampered")
        with self.assertRaises(self.store.PriceError):
            self.store.import_snapshot(html, metadata, self.database_url, self.redis_url)
        self.assertEqual({r["price_krw"] for r in self.lookup()["prices"]}, {6000, 28000})

    def test_future_capture_rejected(self):
        with self.assertRaises(self.store.PriceError):
            self.ingest(observed=datetime.now(timezone.utc) + timedelta(hours=1))
        with self.assertRaises(self.store.PriceNotFound):
            self.lookup()

    def test_mid_batch_database_failure_rolls_back_snapshot_and_rows(self):
        self.ingest()
        with self.pg.connect(self.database_url) as connection:
            connection.execute("""
                CREATE OR REPLACE FUNCTION reject_test_product() RETURNS trigger AS $$
                BEGIN
                    IF NEW.product_id = '134043' THEN
                        RAISE EXCEPTION 'injected storage failure';
                    END IF;
                    RETURN NEW;
                END; $$ LANGUAGE plpgsql;
                CREATE TRIGGER fail_mid_batch BEFORE INSERT ON price_observations
                    FOR EACH ROW EXECUTE FUNCTION reject_test_product();
            """)
        try:
            with self.assertRaisesRegex(self.store.PriceError, "injected storage failure"):
                self.ingest(observed=self.observed + timedelta(minutes=1))
            with self.pg.connect(self.database_url) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM price_snapshots").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT count(*) FROM price_observations").fetchone()[0], 5)
        finally:
            with self.pg.connect(self.database_url) as connection:
                connection.execute("DROP TRIGGER fail_mid_batch ON price_observations")

    def test_concurrent_duplicate_import_commits_exactly_once(self):
        html, metadata = self.capture()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.store.import_snapshot, html, metadata,
                                   self.database_url, self.redis_url) for _ in range(2)]
            results = [future.result(timeout=15) for future in futures]
        self.assertEqual(sorted(result["inserted"] for result in results), [0, 5])
        with self.pg.connect(self.database_url) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM price_snapshots").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM price_observations").fetchone()[0], 5)

    def test_redis_outage_does_not_fall_back_to_database_success(self):
        self.ingest()
        with self.assertRaisesRegex(self.store.PriceError, "(?i)redis|cache"):
            self.lookup(redis_url="redis://127.0.0.1:1/15?socket_connect_timeout=0.2&socket_timeout=0.2")

    def test_database_outage_does_not_return_cached_success(self):
        self.ingest()
        self.lookup()
        with self.assertRaises(self.store.PriceError) as failure:
            self.lookup(database_url="postgresql://tester:DO_NOT_LEAK_THIS@127.0.0.1:1/ygosolver_test_outage?connect_timeout=1")
        self.assertNotIn("DO_NOT_LEAK_THIS", str(failure.exception))

    def test_hit_needs_revision_columns_but_does_not_load_price_rows(self):
        from psycopg import sql
        self.ingest()
        expected = self.lookup()["prices"]
        role = "ygosolver_test_cache_" + uuid4().hex[:10]
        with self.pg.connect(self.database_url) as connection:
            connection.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
            connection.execute(sql.SQL("GRANT SELECT (snapshot_id, card_number, locale) ON price_observations TO {}").format(sql.Identifier(role)))
        separator = "&" if "?" in self.database_url else "?"
        restricted_url = self.database_url + separator + "options=-c%20role%3D" + role
        try:
            hit = self.lookup(database_url=restricted_url)
            self.assertEqual(hit["cache_status"], "hit")
            self.assertEqual(hit["prices"], expected)
            self.cache.flushdb()
            with self.assertRaisesRegex(self.store.PriceError, "(?i)permission denied"):
                self.lookup(database_url=restricted_url)
        finally:
            with self.pg.connect(self.database_url) as connection:
                connection.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
                connection.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))

    def test_invalid_database_row_is_not_returned_or_cached(self):
        self.ingest()
        with self.pg.connect(self.database_url) as connection:
            connection.execute("UPDATE price_observations SET name='' WHERE product_id='134043'")
        with self.assertRaises(self.store.PriceError):
            self.lookup()
        self.assertEqual(self.cache.dbsize(), 0)

    def test_corrupt_cache_fails_instead_of_becoming_a_cache_miss(self):
        self.ingest()
        self.lookup()
        self.cache.set(self.only_cache_key(), "not-json")
        with self.assertRaises(self.store.PriceError):
            self.lookup()

    def test_cached_payload_is_validated(self):
        self.ingest()
        self.lookup()
        key = self.only_cache_key()
        original = json.loads(self.cache.get(key))
        variants = []
        for field, value in [("card_number", "YAC1-JP999"), ("locale", "ko"),
                             ("prices", []), ("revision", -1),
                             ("expires_at", (self.observed + timedelta(days=1)).isoformat())]:
            bad = json.loads(json.dumps(original))
            bad[field] = value
            variants.append(bad)
        for field, value in [("price_krw", 0), ("price_krw", True),
                             ("stock_status", "probably"), ("observed_at", "2026-10-08T00:00:00"),
                             ("observed_at", (datetime.now(timezone.utc) - timedelta(hours=13)).isoformat()),
                             ("observed_at", (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())]:
            bad = json.loads(json.dumps(original))
            bad["prices"][0][field] = value
            variants.append(bad)
        for bad in variants:
            with self.subTest(payload=bad):
                self.cache.set(key, json.dumps(bad))
                with self.assertRaises(self.store.PriceError):
                    self.lookup()


if __name__ == "__main__":
    unittest.main()
