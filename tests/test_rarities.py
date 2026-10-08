"""Codex checks for official print parsing and strict OCG inventory reads."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import inventory
import rarities
from test_catalog import detail_html


def badge(code="SR", rid=3, label="슈퍼 레어"):
    return f'<div class="lr_icon rid_{rid}"><p>{code}</p><span>{label}</span></div>'


def product_row(cid=16537, icons=None):
    icons = badge() if icons is None else icons
    return f'''<div class="t_row"><input class="cid" value="{cid}">
    <input class="link_value" value="/yugiohdb/card_search.action?ope=2&cid={cid}">
    <span class="card_name">Evil★Twin's 트러블 써니</span><div class="icon rarity">{icons}</div></div>'''


def product_html(rows, total=1):
    return f'''<html><body class="ko"><title>검증 상품 | 공식 DB</title>
    <div class="sort_set"><span class="text">전 {total}장</span></div>
    <div id="card_list">{rows}</div></body></html>'''


class RarityParserTest(unittest.TestCase):
    product = {"pid": 123, "name": "검증 상품", "release_date": "2026/01/01"}

    def test_product_card_retains_every_official_badge(self):
        parsed = rarities.parse_product_page(product_html(product_row(icons=badge() + badge("N", 1, "노멀"))),
                                             "ko", self.product)
        self.assertEqual(parsed["total"], 1)
        self.assertEqual(parsed["rows"][0]["cid"], 16537)
        self.assertEqual(parsed["rows"][0]["rarities"],
                         [{"code": "SR", "rid": 3, "label": "슈퍼 레어"},
                          {"code": "N", "rid": 1, "label": "노멀"}])

    def test_wrong_product_count_duplicate_card_and_missing_badges_fail(self):
        valid = product_html(product_row())
        bad_pages = [valid.replace("검증 상품 |", "다른 상품 |"), product_html(product_row(), total=2),
                     product_html(product_row() * 2, total=2), product_html(product_row(icons="")),
                     valid.replace("<p>SR</p>", "<p></p>"),
                     valid.replace("rid_3", "rid_3 rid_4"),
                     valid.replace("cid=16537", "cid=12950"),
                     product_html(product_row(icons=badge() * 2))]
        for html in bad_pages:
            with self.subTest(html=html), self.assertRaises(rarities.RarityError):
                rarities.parse_product_page(html, "ko", self.product)

    def test_detail_print_is_bound_to_card_and_product(self):
        printing = f'''<div class="t_row"><span class="time">2026-01-01</span>
        <span class="card_number">ABC-KR001</span><input class="link_value"
        value="/yugiohdb/card_search.action?ope=1&sess=1&pid=123&rp=99999">
        <div class="icon rarity">{badge()}</div></div>'''
        html = detail_html("검증 카드", cid=16537).replace("</html>",
                  f'<div id="update_list"><div class="t_body">{printing}</div></div></html>')
        parsed = rarities.parse_detail_prints(html, "ko", 16537, "검증 카드")
        self.assertEqual([(p["pid"], p["code"], p["card_number"]) for p in parsed], [(123, "SR", "ABC-KR001")])
        # Older official products can have an empty set number; the field still must exist.
        blank_number_html = html.replace("ABC-KR001", "")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(rarities.catalog, "fetch_html", return_value=blank_number_html) as fetch:
                fetched = rarities.load_detail(root, "ko", 16537, "검증 카드")
                self.assertEqual(fetched["prints"][0]["card_number"], "")
                self.assertEqual(rarities.load_detail(root, "ko", 16537, "검증 카드"), fetched)
                fetch.assert_called_once()
            path = root / "ko/details/16537.json"
            del fetched["prints"][0]["card_number"]
            path.write_text(json.dumps(fetched, ensure_ascii=False), encoding="utf-8")
            with mock.patch.object(rarities.catalog, "fetch_html") as fetch:
                with self.assertRaises(rarities.RarityError):
                    rarities.load_detail(root, "ko", 16537, "검증 카드")
                fetch.assert_not_called()
        for bad, name in [(html, "다른 카드"), (html.replace(badge(), ""), "검증 카드")]:
            with self.subTest(name=name, bad=bad), self.assertRaises(rarities.RarityError):
                rarities.parse_detail_prints(bad, "ko", 16537, name)

    def test_product_resume_validates_cached_count_and_badges_without_refetching(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "ko/products/123.json"
            path.parent.mkdir(parents=True)
            record = {"locale": "ko", "pid": 123, "name": "검증 상품", "total": 2,
                      "url": "https://www.db.yugioh-card.com/yugiohdb/card_search.action?ope=1&sess=1&pid=123&rp=99999&request_locale=ko",
                      "fetched_at": "2026-10-07T15:00:00+00:00", "html_sha256": "a" * 64,
                      "rows": [{"cid": 16537, "name": "검증 카드", "rarities": [{"code": "SR", "rid": 3, "label": "슈퍼 레어"}]}]}
            for bad in (record, {**record, "total": 1, "rows": [{"cid": 16537, "name": "검증 카드", "rarities": []}]}):
                path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
                with mock.patch.object(rarities.catalog, "fetch_html") as fetch:
                    with self.assertRaises(rarities.RarityError):
                        rarities.load_product(root, "ko", self.product)
                    fetch.assert_not_called()


class RarityReadTest(unittest.TestCase):
    def test_lowest_rarity_order_does_not_prefer_holographic_over_ultra(self):
        order = rarities.RARITY_ORDER
        for lower, higher in (("N", "R"), ("R", "SR"), ("SR", "UR"), ("UR", "SE"), ("SE", "HR"),
                              ("UL", "HR"), ("HR", "P+HR")):
            with self.subTest(lower=lower, higher=higher):
                self.assertLess(order.index(lower), order.index(higher))

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "official.sqlite"
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        # Explicit fixture: one two-locale card, one Japan-only, and one TCG-only card.
        self.db.executescript('''
            CREATE TABLE cards (cid INTEGER PRIMARY KEY, name_ko TEXT, name_ja TEXT);
            INSERT INTO cards VALUES (16537, '써니', 'サニー'), (15627, NULL, 'リィラ'), (9, NULL, NULL);
            CREATE TABLE rarity_meta (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO rarity_meta VALUES ('status', '"complete"');
            CREATE TABLE rarity_codes (key TEXT PRIMARY KEY, rid INTEGER, codes TEXT, label_ko TEXT, sort_order INTEGER);
            INSERT INTO rarity_codes VALUES ('N',1,'{"ko":"N","ja":"N"}','노멀',0),
              ('SR',3,'{"ko":"SR","ja":"SR"}','슈퍼 레어',2), ('UR',4,'{"ko":"UR","ja":"UR"}','울트라 레어',3);
            CREATE TABLE rarity_products (locale TEXT, pid INTEGER);
            INSERT INTO rarity_products VALUES ('ko',100),('ja',200);
            CREATE TABLE rarity_prints (locale TEXT, pid INTEGER, cid INTEGER, rid INTEGER, code TEXT, label_local TEXT);
            INSERT INTO rarity_prints VALUES ('ko',100,16537,3,'SR','슈퍼 레어'), ('ja',200,16537,1,'N','ノーマル'),
              ('ja',200,16537,3,'SR','スーパー'), ('ja',200,15627,4,'UR','ウルトラ'), ('ja',200,15627,3,'SR','スーパー');
            CREATE TABLE rarity_coverage (locale TEXT, cid INTEGER, status TEXT, evidence_url TEXT, evidence_sha256 TEXT);
            INSERT INTO rarity_coverage VALUES ('ko',16537,'printed',NULL,NULL),('ja',16537,'printed',NULL,NULL),
              ('ja',15627,'printed',NULL,NULL);
        ''')
        self.db.commit()

    def mutate(self, sql):
        self.db.execute(sql)
        self.db.commit()

    def test_locale_union_deduplicates_sorts_and_does_not_invent_normal(self):
        self.assertEqual(rarities.get_rarities(16537, self.path), ["N", "SR"])
        self.assertEqual(rarities.get_rarities(15627, self.path), ["SR", "UR"])

    def test_distinct_finishes_with_same_printed_code_remain_separate(self):
        self.mutate("INSERT INTO rarity_codes VALUES ('SE',5,'{\"ko\":\"SE\",\"ja\":\"SE\"}','시크릿 레어',20)")
        self.mutate("INSERT INTO rarity_codes VALUES ('SE@43',43,'{\"ko\":\"SE\",\"ja\":\"SE\"}','시크릿 레어（SPECIAL BLUE Ver.）',21)")
        self.mutate("INSERT INTO rarity_prints VALUES ('ko',100,16537,5,'SE','시크릿 레어')")
        self.mutate("INSERT INTO rarity_prints VALUES ('ja',200,16537,43,'SE','シークレットレア仕様（SPECIAL BLUE Ver.）')")
        self.assertEqual(rarities.get_rarities(16537, self.path), ["N", "SR", "SE", "SE@43"])

    def test_wrong_printed_code_for_valid_rid_is_rejected(self):
        self.mutate("UPDATE rarity_prints SET code='UR' WHERE rid=3")
        with self.assertRaises(rarities.RarityError):
            rarities.get_rarities(16537, self.path)

    def test_missing_db_unknown_cid_and_invalid_id_do_not_create_data(self):
        absent = self.path.with_name("missing.sqlite")
        with self.assertRaises(rarities.RarityError):
            rarities.get_rarities(16537, absent)
        self.assertFalse(absent.exists())
        for cid in (999, None, True, 16537.0, "16537", -1):
            with self.subTest(cid=cid), self.assertRaises(rarities.RarityError):
                rarities.get_rarities(cid, self.path)

    def test_no_ocg_edition_is_explicit_unavailable(self):
        with self.assertRaises(rarities.RarityUnavailable):
            rarities.get_rarities(9, self.path)

    def test_incomplete_status_and_partial_locale_coverage_fail(self):
        self.mutate("UPDATE rarity_meta SET value='\"incomplete\"' WHERE key='status'")
        with self.assertRaises(rarities.RarityError):
            rarities.get_rarities(16537, self.path)
        self.mutate("UPDATE rarity_meta SET value='\"complete\"' WHERE key='status'")
        self.mutate("DELETE FROM rarity_coverage WHERE locale='ja' AND cid=16537")
        with self.assertRaises(rarities.RarityError):
            rarities.get_rarities(16537, self.path)

    def test_one_printed_locale_cannot_hide_other_locale_missing_prints(self):
        self.mutate("DELETE FROM rarity_prints WHERE locale='ja' AND cid=16537")
        with self.assertRaises(rarities.RarityError):
            rarities.get_rarities(16537, self.path)

    def test_tcg_print_contamination_and_unknown_code_fail(self):
        self.mutate("INSERT INTO rarity_prints VALUES ('en',300,16537,4,'UR','Ultra Rare')")
        with self.assertRaises(rarities.RarityError):
            rarities.get_rarities(16537, self.path)
        self.mutate("DELETE FROM rarity_prints WHERE locale='en'")
        self.mutate("UPDATE rarity_prints SET rid=999, code='NEW_UNKNOWN' WHERE locale='ko'")
        with self.assertRaises(rarities.RarityError):
            rarities.get_rarities(16537, self.path)

    def test_inventory_rebuild_refuses_to_destroy_existing_rarity_data(self):
        target = self.path.with_name("official_cards.sqlite")
        self.db.close()
        self.path.rename(target)
        before = target.read_bytes()
        with mock.patch.object(inventory, "crawl_locale") as crawl:
            with self.assertRaisesRegex(inventory.InventoryError, "rarity"):
                inventory.build_inventory("new-snapshot", target.parent)
            crawl.assert_not_called()
        self.assertEqual(target.read_bytes(), before)

    def test_failed_enrichment_transaction_preserves_previous_rarity_rows(self):
        before = self.db.execute("SELECT * FROM rarity_prints ORDER BY locale,pid,cid,rid").fetchall()
        product = {"pid": 123, "name": "검증 상품", "release_date": "2026/01/01"}
        record = {"url": "https://www.db.yugioh-card.com/", "total": 1,
                  "fetched_at": "2026-10-07T15:00:00+00:00", "html_sha256": "a" * 64,
                  "rows": [{"cid": 999999, "name": "Unknown", "rarities": [{"rid": 3, "code": "SR", "label": "슈퍼 레어"}]}]}
        report = {"status": "complete", "snapshot": "test", "inventory_snapshot": "test", "finished_at": "2026-10-07"}
        with self.assertRaises((rarities.RarityError, sqlite3.IntegrityError)):
            rarities.write_rarity_tables(self.path, report,
                                        {"ko": {"products": [product]}, "ja": {"products": []}},
                                        {("ko", 123): record}, {("ko", 999999): {(123, 3)}}, {},
                                        {"ko": [], "ja": []})
        after = self.db.execute("SELECT * FROM rarity_prints ORDER BY locale,pid,cid,rid").fetchall()
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
