"""Public source evidence and independent corrupt-response checks for the bulk parsers."""

import gzip
import hashlib
import json
from pathlib import Path
import re
import unittest

import shop_catalog_parse as parser
from shop_catalog_store import ShopCatalogError


FIXTURES = Path(__file__).parent / "fixtures" / "shop_catalog"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))


def fixture(name):
    body = gzip.decompress((FIXTURES / f"{name}.html.gz").read_bytes())
    if hashlib.sha256(body).hexdigest() != MANIFEST[name]["fixture_sha256"]:
        raise AssertionError(f"fixture integrity failure: {name}")
    return body


def parse(name, body=None, url=None):
    body = fixture(name) if body is None else body
    url = MANIFEST[name]["url"] if url is None else url
    if name.startswith("cardmoa"):
        size = None
        if MANIFEST[name]["page_number"] > 1:
            size = 140 if name.startswith("cardmoa_ko_") else 91
        return parser.parse_cardmoa_page(body, url, page_size=size)
    return parser.parse_tcgshop_page(body, url)


class CatalogSourceContracts(unittest.TestCase):
    def test_all_source_pages_have_exact_declared_coverage(self):
        for name, expected in MANIFEST.items():
            with self.subTest(name=name):
                actual = parse(name)
                for field in ("total_products", "total_pages", "page_number"):
                    self.assertEqual(actual[field], expected[field])
                products = actual["products"]
                self.assertEqual(len(products), expected["product_count"])
                self.assertEqual(len({row["product_id"] for row in products}), len(products))
                self.assertTrue(all(row["raw"]["locale_evidence"] for row in products))

    def test_cardmoa_discounted_sold_out_card_is_preserved(self):
        rows = {row["product_id"]: row for row in parse("cardmoa_ko_first")["products"]}
        row = rows["1318026188"]
        self.assertEqual(row["name"], "성스러운마술사")
        self.assertEqual((row["price_krw"], row["list_price_krw"]), (1200, 1500))
        self.assertEqual(row["stock_status"], "out_of_stock")
        self.assertEqual(row["card_number"], "BP1-KR163")
        self.assertEqual(row["rarity_label"], "레어")
        self.assertEqual(row["locale"], "ko")

    def test_tcgshop_discount_and_no_cart_evidence_remain_distinct(self):
        rows = {row["product_id"]: row for row in parse("tcgshop_ko_first")["products"]}
        row = rows["69208"]
        self.assertEqual((row["price_krw"], row["list_price_krw"]), (240, 300))
        self.assertEqual((row["name"], row["card_number"]), ("미러클퓨전", "DP23-KR020"))
        self.assertEqual(row["stock_status"], "in_stock")

    def test_discontinued_unpriced_products_are_not_dropped_or_zero_priced(self):
        products = parse("tcgshop_ko_last")["products"]
        self.assertEqual(len(products), 40)
        self.assertTrue(all(row["price_krw"] is None for row in products))
        self.assertTrue(all(row["stock_status"] == "unknown" for row in products))
        self.assertTrue(all(row["raw"]["price_error"] for row in products))
        rows = {row["product_id"]: row for row in products}
        self.assertEqual(rows["3842"]["card_number"], "SRL-K080")

    def test_misfiled_japanese_card_does_not_inherit_korean_category(self):
        products = parse("cardmoa_ko_condition")["products"]
        row = next(row for row in products if row["product_id"] == "1318035120")
        self.assertEqual(row["locale"], "ja")
        self.assertEqual(row["card_number"], "EEN-JP037")
        self.assertIn("B급카드", str(row["raw"]))

    def test_mixed_condition_category_preserves_each_edition(self):
        rows = {row["product_id"]: row for row in parse("tcgshop_condition")["products"]}
        self.assertEqual(rows["125398"]["locale"], "ko")
        self.assertEqual(rows["97509"]["locale"], "ja")
        self.assertEqual(rows["125398"]["price_krw"], 3150)
        self.assertIn("A급", str(rows["97509"]["raw"]))

    def test_overframe_finishes_are_not_collapsed_to_the_base_rarity(self):
        rows = parse("tcgshop_ja_first")["products"]
        labels = {row.get("rarity_label") for row in rows}
        self.assertTrue({"UR OverFrame", "PSC OverFrame", "Ultra Rare", "Prismatic Secret Rare"} <= labels)
        for row in parse("tcgshop_condition")["products"]:
            self.assertNotIn("A급", row.get("rarity_label", ""))

    def test_additional_finish_text_is_not_silently_discarded(self):
        original = fixture("tcgshop_ko_first")
        body, changed = re.subn(rb'(class="glist_03"[^>]*>\s*Normal</span>)',
                                rb'\1<span class="glist_03">Unverified Special Finish</span>', original, count=1)
        self.assertEqual(changed, 1)
        rows = parse("tcgshop_ko_first", body)["products"]
        self.assertTrue(any("Unverified Special Finish" in row.get("rarity_label", "") for row in rows))

    def test_missing_region_is_explicitly_unresolved(self):
        row = next(row for row in parse("tcgshop_ja_last")["products"] if row["product_id"] == "466")
        self.assertEqual(row["card_number"], "SK2-038")
        self.assertIsNone(row["locale"])
        self.assertTrue(row["raw"]["locale_evidence"])

    def test_cardmoa_missing_rarity_does_not_drop_product(self):
        products = parse("cardmoa_ja_first")["products"]
        row = next(row for row in products if row.get("card_number") == "SSB1-JP")
        self.assertEqual(row["name"], "시크릿샤이니박스")
        self.assertTrue(row["raw"])
        self.assertTrue(not row.get("rarity_label"))

    def test_accessory_misfiled_as_single_card_keeps_price_without_invented_identity(self):
        row = next(row for row in parse("cardmoa_ko_page17")["products"] if row["product_id"] == "1623460051")
        self.assertEqual((row["price_krw"], row["list_price_krw"]), (1000, 7000))
        self.assertEqual(row["name"], "쿼터센츄리_탑로딩카드홀더_골드")
        self.assertIsNone(row["locale"])
        self.assertIsNone(row.get("card_number"))
        self.assertIsNone(row.get("rarity_label"))
        self.assertTrue(row["raw"]["locale_evidence"])

    def test_rarity_without_printed_number_stays_unresolved(self):
        row = next(row for row in parse("cardmoa_ja_page160")["products"] if row["product_id"] == "1561637852")
        self.assertEqual((row["price_krw"], row["list_price_krw"]), (12000, 15000))
        self.assertEqual(row["rarity_label"], "페러럴레어")
        self.assertIsNone(row.get("card_number"))
        self.assertIsNone(row["locale"])
        self.assertTrue(row["raw"]["locale_evidence"])

    def test_malformed_present_card_number_cannot_be_reinterpreted_as_a_rarity(self):
        for name, before in (("cardmoa_ko_first", b"(BP1-KR163)"),
                             ("cardmoa_ja_page160", "페러럴레어".encode("euc-kr"))):
            original = fixture(name)
            start = original.index(b"name='fitem1561637852'") if name == "cardmoa_ja_page160" else 0
            broken = original[:start] + original[start:].replace(before, b"BP1-KR163", 1)
            self.assertNotEqual(broken, original)
            with self.subTest(name=name), self.assertRaises(ShopCatalogError):
                parse(name, broken)

    def test_unrelated_mixed_encoding_is_separate_from_invalid_product_text(self):
        body = fixture("cardmoa_ko_first")
        mixed_chrome = b'<nav title="' + "전체카드보기".encode("utf-8") + b'">menu</nav>'
        self.assertEqual(len(parse("cardmoa_ko_first", mixed_chrome + body)["products"]), 140)
        broken = body.replace("성스러운마술사".encode("euc-kr"), b"\xff", 1)
        with self.assertRaises(ShopCatalogError):
            parse("cardmoa_ko_first", broken)

    def test_wrong_category_or_current_page_is_rejected(self):
        for name, bad_url in (
            ("cardmoa_ko_first", "http://cardmoa.com/shop/list.php?ca_id=102010"),
            ("cardmoa_ko_first", "http://cardmoa.com/shop/list.php?ca_id=101010&page=2"),
            ("tcgshop_ko_first", "http://www.tcgshop.co.kr/goods_list.php?Index=288"),
            ("tcgshop_ko_last", MANIFEST["tcgshop_ko_first"]["url"]),
        ):
            with self.subTest(name=name, bad_url=bad_url), self.assertRaises(ShopCatalogError):
                parse(name, url=bad_url)

    def test_cardmoa_hidden_visible_price_disagreement_fails(self):
        body = fixture("cardmoa_ko_first").replace(b"name=it_amount value='1200'", b"name=it_amount value='9999'", 1)
        self.assertNotEqual(body, fixture("cardmoa_ko_first"))
        with self.assertRaises(ShopCatalogError):
            parse("cardmoa_ko_first", body)

    def test_displayed_name_is_authoritative_and_hidden_name_remains_evidence(self):
        original = fixture("cardmoa_ko_first")
        before = "name=it_name value='성스러운마술사'".encode("euc-kr")
        body = original.replace(before, b"name=it_name value='truncated'", 1)
        self.assertNotEqual(body, original)
        row = next(row for row in parse("cardmoa_ko_first", body)["products"]
                   if row["product_id"] == "1318026188")
        self.assertEqual(row["name"], "성스러운마술사")
        self.assertIn("truncated", str(row["raw"]))

    def test_structurally_missing_or_duplicate_products_fail_coverage(self):
        for name, before, after in (
            ("cardmoa_ko_first", b"name='fitem1318026188'", b"name='removed1318026188'"),
            ("tcgshop_ko_first", b'id="list_card_69208"', b'id="removed_69208"'),
        ):
            with self.subTest(name=name):
                original = fixture(name)
                body = original.replace(before, after, 1)
                self.assertNotEqual(body, original)
                with self.assertRaises(ShopCatalogError):
                    parse(name, body)

    def test_invalid_numeric_price_is_not_accepted(self):
        for text in ("1,2,00", "-240", "240.5"):
            original = fixture("tcgshop_ko_first")
            body, changed = re.subn(rb'(class="glist_price12">)240',
                                    lambda match: match[1] + text.encode("ascii"), original, count=1)
            self.assertEqual(changed, 1)
            with self.subTest(text=text), self.assertRaises(ShopCatalogError):
                parse("tcgshop_ko_first", body)

    def test_unknown_shop_category_and_offsite_urls_are_rejected(self):
        for shop, category in (("other", "276"), ("tcgshop", "274"), ("cardmoa", "103010")):
            with self.subTest(shop=shop, category=category), self.assertRaises((ShopCatalogError, ValueError)):
                parser.page_url(shop, category)
        with self.assertRaises(ShopCatalogError):
            parse("tcgshop_ko_first", url="http://evil.example/goods_list.php?Index=276")

    def test_html_error_page_is_not_an_empty_catalog(self):
        for name in ("cardmoa_ko_first", "tcgshop_ko_first"):
            with self.subTest(name=name), self.assertRaises(ShopCatalogError):
                parse(name, b"<html><h1>Temporary error</h1></html>")

    def test_disappearing_products_and_navigation_are_not_inferred_as_zero(self):
        original = fixture("tcgshop_ko_first")
        start = "<!-- 상품 목록 시작 -->".encode("euc-kr")
        end = "<!-- 상품 목록 끝 -->".encode("euc-kr")
        before, rest = original.split(start)
        _, after = rest.split(end)
        body = before + start + b"<tr><td> </td></tr>" + end + after
        body, changed = re.subn(
            rb"<table border=0 cellpadding=0 cellspacing=2 id=nom>.*?</table>",
            b"<table border=0 cellpadding=0 cellspacing=2 id=nom><tr><td><font>1</font></td></tr></table>",
            body, count=1, flags=re.DOTALL)
        self.assertEqual(changed, 1)
        with self.assertRaises(ShopCatalogError):
            parse("tcgshop_ko_first", body)

    def test_cardmoa_sold_out_and_cart_button_contradiction_fails(self):
        original = fixture("cardmoa_ko_first")
        marker = b"name=it_amount value='1200'>"
        injected = (b'<img src="../images/go_cart.gif" '
                    b'onclick="fitemcheck(document.fitem1318026188, \'cart_update\');">')
        body = original.replace(marker, marker + injected, 1)
        self.assertNotEqual(body, original)
        with self.assertRaises(ShopCatalogError):
            parse("cardmoa_ko_first", body)
