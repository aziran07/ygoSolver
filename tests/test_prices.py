"""Independent price-ingestion contracts; all HTTP evidence is local."""

import hashlib
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from bs4 import BeautifulSoup

import prices


JA_URL = "http://www.tcgshop.co.kr/goods_list.php?Index=288"
KO_URL = "http://www.tcgshop.co.kr/goods_list.php?Index=276"
OBSERVED_AT = "2026-10-08T07:19:37.504356+00:00"
FIXTURE = Path(__file__).parent / "fixtures" / "tcgshop_ja_list.html"


class PriceContracts(unittest.TestCase):
    def setUp(self):
        self.html = FIXTURE.read_text(encoding="utf-8")

    def soup(self):
        return BeautifulSoup(self.html, "html.parser")

    def parse(self, html=None, url=JA_URL):
        return prices.parse_tcgshop_list(self.html if html is None else str(html), url)

    def test_actual_discounted_prices_and_finish_identity(self):
        rows = self.parse()
        self.assertEqual(len(rows), 5)
        by_id = {row["product_id"]: row for row in rows}
        self.assertEqual(set(by_id), {"39577", "134073", "134053", "134043", "134008"})
        row = by_id["39577"]
        self.assertEqual(row["name"], "확산하는파동")
        self.assertEqual(row["card_number"], "15AY-JPB22")
        self.assertEqual(row["rarity_label"], "Normal")
        self.assertEqual(row["price_krw"], 240)
        self.assertEqual(row["product_url"], "http://www.tcgshop.co.kr/goods_detail.php?goodsIdx=39577")
        self.assertEqual({r["locale"] for r in rows}, {"ja"})
        self.assertEqual({r["stock_status"] for r in rows}, {"in_stock"})
        self.assertTrue(all(r["stock_evidence"] for r in rows))
        self.assertEqual(
            {(r["rarity_label"], r["price_krw"]) for r in rows if r["card_number"] == "YAC1-JP002"},
            {("PSC OverFrame", 28000), ("UR OverFrame", 6000)},
        )

    def test_korean_edition_is_separate(self):
        html = self.html.replace('value="288"', 'value="276"').replace("-JP", "-KR")
        self.assertEqual({r["locale"] for r in self.parse(html, KO_URL)}, {"ko"})
        with self.assertRaises(prices.PriceError):
            self.parse(html, JA_URL)
        with self.assertRaises(prices.PriceError):
            self.parse(self.html, KO_URL)

    def test_all_nonempty_rarity_text_is_preserved(self):
        soup = self.soup()
        soup.select_one("#list_card_39577 .glist_03").find_next_sibling("span").string = "Parallel"
        self.assertEqual(self.parse(soup)[0]["rarity_label"], "Normal Parallel")

    def test_missing_required_fields_and_bad_prices_fail(self):
        for selector in (".glist_01", ".glist_02", ".glist_03", ".glist_price12"):
            with self.subTest(missing=selector):
                soup = self.soup()
                for node in soup.select("#list_card_39577 " + selector):
                    node.decompose()
                with self.assertRaises(prices.PriceError):
                    self.parse(soup)
        for bad in ("", "무료문의", "-240", "1,2,00", "240.5", "$240"):
            with self.subTest(price=bad):
                soup = self.soup()
                soup.select_one(".glist_price12").string = bad
                with self.assertRaises(prices.PriceError):
                    self.parse(soup)

    def test_empty_response_and_duplicate_product_are_errors(self):
        with self.assertRaises(prices.PriceError):
            self.parse("<html>Service unavailable</html>")
        soup = self.soup()
        duplicate = BeautifulSoup(str(soup.select_one("#list_card_39577")), "html.parser")
        soup.body.append(duplicate)
        with self.assertRaises(prices.PriceError):
            self.parse(soup)

    def test_wrong_goods_link_and_wrong_cart_identity_are_errors(self):
        for selector, attr, value in (
            (".glist_01 a", "href", "goods_detail.php?goodsIdx=99999"),
            ('img[src$="/go_cart.gif"]', "onclick", "javascript:cartOneGo('99999');"),
        ):
            with self.subTest(field=attr):
                soup = self.soup()
                soup.select_one("#list_card_39577 " + selector)[attr] = value
                with self.assertRaises(prices.PriceError):
                    self.parse(soup)

    def test_price_alone_and_disabled_cart_do_not_establish_stock(self):
        for disabled in (False, True):
            with self.subTest(disabled=disabled):
                soup = self.soup()
                block = soup.select_one("#list_card_39577")
                cart = block.select_one('img[src$="/go_cart.gif"]')
                if disabled:
                    cart["src"] = "http://www.tcgshop.co.kr/image/good/dis_go_cart.gif"
                    cart.attrs.pop("onclick")
                else:
                    cart.decompose()
                self.assertEqual(self.parse(soup)[0]["stock_status"], "unknown")

    def test_explicit_soldout_keeps_price_and_conflict_fails(self):
        # Synthetic semantic contract: the live sample contains no soldout row.
        soup = self.soup()
        block = soup.select_one("#list_card_39577")
        label = soup.new_tag("span")
        label.string = "품절"
        block.append(label)
        with self.assertRaises(prices.PriceError):
            self.parse(soup)
        block.select_one('img[src$="/go_cart.gif"]').decompose()
        row = self.parse(soup)[0]
        self.assertEqual(row["stock_status"], "out_of_stock")
        self.assertEqual(row["price_krw"], 240)

    def test_unrelated_or_untrusted_source_is_rejected(self):
        for url in (
            "http://other.example/goods_list.php?Index=288",
            "http://www.tcgshop.co.kr/goods_list.php?Index=442",
            "http://www.tcgshop.co.kr/search_result.php?Index=288",
            "http://www.tcgshop.co.kr/goods_list.php?Index=288&Index=276",
        ):
            with self.subTest(url=url), self.assertRaises(prices.PriceError):
                self.parse(url=url)

    def test_currency_and_page_category_must_agree_with_claimed_identity(self):
        soup = self.soup()
        soup.select_one(".glist_price_won").string = "USD"
        with self.assertRaises(prices.PriceError):
            self.parse(soup)
        soup = self.soup()
        soup.select_one('input[name="Index"]')["value"] = "442"
        with self.assertRaises(prices.PriceError):
            self.parse(soup)

    def write_snapshot(self, directory, html=None, **overrides):
        body = (self.html if html is None else html).encode("euc-kr")
        html_path = directory / "response.bin"
        meta_path = directory / "response.json"
        html_path.write_bytes(body)
        metadata = {
            "url": JA_URL, "observed_at": OBSERVED_AT, "status_code": 200,
            "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body),
            "content_type": "text/html",
        }
        metadata.update(overrides)
        meta_path.write_text(json.dumps(metadata), encoding="utf-8")
        return html_path, meta_path

    def database_dump(self, path):
        with closing(sqlite3.connect(path)) as connection:
            return "\n".join(connection.iterdump())

    def test_snapshot_import_is_idempotent_and_preserves_observation_time(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self.write_snapshot(root)
            db = root / "prices.sqlite"
            prices.import_snapshot(*paths, db)
            before = self.database_dump(db)
            self.assertIn(OBSERVED_AT, before)
            self.assertIn("PSC OverFrame", before)
            self.assertIn("UR OverFrame", before)
            prices.import_snapshot(*paths, db)
            self.assertEqual(self.database_dump(db), before)

    def test_bad_evidence_and_late_bad_row_leave_existing_database_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "prices.sqlite"
            prices.import_snapshot(*self.write_snapshot(root), db)
            before = self.database_dump(db)
            for invalid in (
                {"sha256": "0" * 64}, {"bytes": 1}, {"status_code": 429},
                {"observed_at": "not-a-date"}, {"observed_at": "2026-10-08T07:19:37"},
                {"observed_at": "2099-01-01T00:00:00+00:00"},
            ):
                with self.subTest(metadata=invalid):
                    paths = self.write_snapshot(root, **invalid)
                    with self.assertRaises(prices.PriceError):
                        prices.import_snapshot(*paths, db)
                    self.assertEqual(self.database_dump(db), before)
            soup = self.soup()
            soup.select(".glist_price12")[-1].string = "invalid"
            with self.assertRaises(prices.PriceError):
                prices.import_snapshot(*self.write_snapshot(root, str(soup)), db)
            self.assertEqual(self.database_dump(db), before)

    def test_new_observation_keeps_prior_price_and_stock_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "prices.sqlite"
            prices.import_snapshot(*self.write_snapshot(root), db)
            soup = self.soup()
            block = soup.select_one("#list_card_39577")
            block.select_one(".glist_price12").string = "200"
            block.select_one('img[src$="/go_cart.gif"]').decompose()
            label = soup.new_tag("span")
            label.string = "품절"
            block.append(label)
            paths = self.write_snapshot(root, str(soup), observed_at="2026-10-08T07:20:00+00:00")
            prices.import_snapshot(*paths, db)
            with closing(sqlite3.connect(db)) as connection:
                history = connection.execute(
                    "SELECT p.price_krw, p.stock_status, s.observed_at FROM price_observations p "
                    "JOIN snapshots s ON s.id=p.snapshot_id WHERE p.product_id='39577' ORDER BY s.observed_at"
                ).fetchall()
                self.assertEqual(connection.execute("SELECT count(*) FROM price_observations").fetchone()[0], 10)
            self.assertEqual(history, [
                (240, "in_stock", OBSERVED_AT),
                (200, "out_of_stock", "2026-10-08T07:20:00+00:00"),
            ])

    def test_database_write_failure_rolls_back_entire_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "prices.sqlite"
            prices.import_snapshot(*self.write_snapshot(root), db)
            with closing(sqlite3.connect(db)) as connection:
                connection.execute(
                    "CREATE TRIGGER fail_observation BEFORE INSERT ON price_observations "
                    "WHEN NEW.product_id='134043' BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END"
                )
            before = self.database_dump(db)
            paths = self.write_snapshot(root, observed_at="2026-10-08T07:20:00+00:00")
            with self.assertRaises((prices.PriceError, sqlite3.DatabaseError)):
                prices.import_snapshot(*paths, db)
            self.assertEqual(self.database_dump(db), before)


if __name__ == "__main__":
    unittest.main()
