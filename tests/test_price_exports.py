"""Codex contract for displayed prices in files; numeric expected values are independent."""
import csv
import io
import unittest
from datetime import datetime, timedelta, timezone

from openpyxl import load_workbook
import exports

PRICE_HEADERS = ["단가(원)", "합계(원)", "가격 상태", "가격 상세", "가격 판본", "가격 수록 번호",
                 "가격 상품 URL", "가격 관찰 시각(KST)"]


def quote(status="ok"):
    observed = datetime(2026, 10, 8, 7, 19, 37, tzinfo=timezone.utc)
    return {"status": status, "detail": "수집 상품 중 확인된 재고 최저가", "locale": "ja",
            "unit_price_krw": 240 if status == "ok" else None,
            "card_number": "15AY-JPB22" if status == "ok" else None,
            "product_id": "39577" if status == "ok" else None,
            "product_url": "http://www.tcgshop.co.kr/goods_detail.php?goodsIdx=39577" if status == "ok" else None,
            "observed_at": observed if status == "ok" else None,
            "expires_at": observed + timedelta(hours=12) if status == "ok" else None,
            "matched_products": 1, "in_stock_products": 1 if status == "ok" else 0,
            "excluded_unverified": 0}


def entry(quantity=1, price=None):
    return {"cid": 5631, "name": "확산하는 파동", "rarity": "N", "quantity": quantity,
            "name_ko": "확산하는 파동", "name_ja": "拡散する波動", "name_en": "Diffusion Wave-Motion",
            "price": quote() if price is None else price}


class PriceExportTest(unittest.TestCase):
    def test_price_columns_numeric_total_and_provenance_in_both_formats(self):
        cards = exports.aggregate([entry(2), entry(3)])
        rows = list(csv.reader(io.StringIO(exports.to_csv_bytes(cards, include_price=True).decode("utf-8-sig"))))
        self.assertEqual(rows[0], ["카드명", "레어도", "수량", *PRICE_HEADERS])
        self.assertEqual(rows[1][:6], ["확산하는 파동", "노멀", "5", "240", "1200", "ok"])
        self.assertEqual(rows[1][-4:], ["ja", "15AY-JPB22",
            "http://www.tcgshop.co.kr/goods_detail.php?goodsIdx=39577", "2026-10-08T16:19:37+09:00"])
        sheet = load_workbook(io.BytesIO(exports.to_xlsx_bytes(cards, include_price=True))).active
        self.assertEqual(sheet["D2"].value, 240)
        self.assertEqual(sheet["E2"].value, 1200)
        self.assertEqual(sheet["D2"].data_type, "n")

    def test_nonprice_status_has_blank_amounts_and_preserves_reason(self):
        for status in ("no_stock", "not_observed", "unverified", "expired", "config_error",
                       "database_error", "cache_error", "data_error", "official_error"):
            with self.subTest(status=status):
                q = {**quote(status), "detail": "=explicit reason"}
                cards = [entry(price=q)]
                rows = list(csv.reader(io.StringIO(exports.to_csv_bytes(cards, include_price=True).decode("utf-8-sig"))))
                self.assertEqual(rows[1][3:7], ["", "", status, "'=explicit reason"])
                sheet = load_workbook(io.BytesIO(exports.to_xlsx_bytes(cards, include_price=True))).active
                self.assertIsNone(sheet["D2"].value)
                self.assertIsNone(sheet["E2"].value)
                self.assertEqual(sheet["G2"].value, "=explicit reason")
                self.assertEqual(sheet["G2"].data_type, "s")

    def test_aggregation_refuses_conflicting_prices_or_language(self):
        for changed in ({"unit_price_krw": 300}, {"locale": "ko"}, {"status": "expired", "unit_price_krw": None}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                exports.aggregate([entry(), entry(price={**quote(), **changed})])

    def test_price_enabled_export_does_not_invent_missing_or_invalid_prices(self):
        missing = entry()
        del missing["price"]
        bad_quotes = [None, {}, {**quote(), "unit_price_krw": 0}, {**quote(), "unit_price_krw": True},
                      {**quote(), "unit_price_krw": 2.5}, {**quote("expired"), "unit_price_krw": 240}]
        for card in [missing, *[{**entry(), "price": q} for q in bad_quotes]]:
            for writer in (exports.to_csv_bytes, exports.to_xlsx_bytes):
                with self.subTest(card=card, writer=writer.__name__), self.assertRaises(ValueError):
                    writer([card], include_price=True)

    def test_aggregation_cannot_assign_one_rows_price_to_an_unpriced_copy(self):
        priced = entry()
        unpriced = entry()
        del unpriced["price"]
        for entries in ([priced, unpriced], [unpriced, priced]):
            with self.subTest(order=["price" in e for e in entries]), self.assertRaises(ValueError):
                exports.aggregate(entries)


if __name__ == "__main__":
    unittest.main()
