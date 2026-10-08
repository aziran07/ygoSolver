"""Deck list aggregation and XLSX / CSV export."""

import csv
import io
from datetime import datetime, timedelta, timezone

from openpyxl import Workbook
from openpyxl.utils import get_column_letter

import card_prices
import deck_order
import rarities

# Columns the user can add to the export, in their fixed output order after 카드명, 레어도, 수량.
OPTIONAL_COLUMNS = {"name_ko": "한국어 이름", "name_ja": "일본어 이름", "name_en": "영어 이름", "cid": "공식 CID"}

# Price columns appended after the optional columns when include_price is set. Each card then carries "price",
# the card_prices.get_card_price result; amounts and observed time are filled for card_prices.PRICED_STATUSES ("ok" in
# stock, "out_of_stock" sold out, "stock_unknown" stock not verifiable), every other status leaves them blank.
# 단가 and 합계 are always included; the price metadata columns the user can add follow them in this fixed order.
# The price detail and the price edition are never exported.
PRICE_HEADERS = ["단가(원)", "합계(원)"]
OPTIONAL_PRICE_COLUMNS = {"status": "가격 상태", "product_url": "가격 상품 URL", "observed_at": "가격 관찰 시각(KST)",
                          "card_number": "가격 수록 번호"}
PRICE_FIELDS = {"status", "detail", "locale", "unit_price_krw", "card_number", "product_url", "observed_at"}
KST = timezone(timedelta(hours=9), "KST")

# Spreadsheet apps treat cells starting with these characters as formulas.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def validate_quantity(value):
    """Return value if it is a positive whole int; bool, float and Fraction are rejected."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"수량은 1 이상의 정수여야 합니다: {value!r}")
    if value < 1:
        raise ValueError(f"수량은 1 이상이어야 합니다: {value}")
    return value


def aggregate(entries):
    """Sum quantities of entries sharing the same official CID and rarity.

    Each entry: {'cid': int, 'rarity': rarity code, 'name': str, 'name_ko', 'name_ja', 'name_en', 'quantity': int}.
    The same CID with different rarities stays on separate lines, but every line of one CID must
    carry the same card name. Returns one dict per (CID, rarity) in first-seen order.
    """
    by_cid_rarity = {}
    name_by_cid = {}
    for entry in entries:
        cid = entry["cid"]
        if isinstance(cid, bool) or not isinstance(cid, int) or cid <= 0:
            raise ValueError(f"올바른 공식 CID(양의 정수)가 없는 항목은 내보낼 수 없습니다: {entry['name']!r} (CID {cid!r})")
        quantity = validate_quantity(entry["quantity"])
        name = entry["name"].strip()
        if not name:
            raise ValueError(f"CID {cid} 항목의 카드명이 비어 있습니다.")
        rarity = entry.get("rarity")
        if rarity not in rarities.RARITY_LABELS:
            raise ValueError(f"CID {cid} 항목의 레어도가 올바르지 않습니다: {rarity!r}. 레어도를 선택하세요.")
        if key_price_conflicts(by_cid_rarity.get((cid, rarity)), entry):
            raise ValueError(f"같은 카드·레어도(CID {cid}, {rarity})에 서로 다른 가격 정보가 들어왔습니다. "
                             "가격 판본과 가격을 다시 확인하세요.")
        if name_by_cid.setdefault(cid, name) != name:
            raise ValueError(
                f"같은 카드(CID {cid})에 서로 다른 카드명이 입력되었습니다: "
                f"{name_by_cid[cid]!r} / {name!r}. 하나로 맞춰 주세요."
            )
        key = (cid, rarity)
        if key not in by_cid_rarity:
            by_cid_rarity[key] = {**entry, "name": name, "quantity": 0}
        by_cid_rarity[key]["quantity"] += quantity
    return list(by_cid_rarity.values())


def key_price_conflicts(existing, entry):
    """True when an already aggregated line of the same CID and rarity carries other price data than entry
    (including one with a price and one without)."""
    return existing is not None and existing.get("price") != entry.get("price")


def sort_cards(cards, database_path=deck_order.DATABASE_PATH):
    """Return a new list of the aggregated cards in deck order (see deck_order), the same CID's rarities lowest first.

    Raises deck_order.DeckOrderError (a ValueError) when any card's official order data is missing or invalid.
    """
    keys = deck_order.get_sort_keys([card["cid"] for card in cards], database_path=database_path)
    return sorted(cards, key=lambda card: (keys[card["cid"]], rarities.RARITY_ORDER.index(card["rarity"])))


def selected_fields(optional_fields):
    """Return the chosen optional fields in canonical order; unknown fields raise ValueError."""
    unknown = set(optional_fields) - OPTIONAL_COLUMNS.keys()
    if unknown:
        raise ValueError(f"알 수 없는 내보내기 열입니다: {sorted(unknown)!r}")
    return [field for field in OPTIONAL_COLUMNS if field in optional_fields]


def selected_price_fields(optional_price_fields, include_price):
    """Return the chosen price metadata fields in canonical order.

    Unknown fields raise ValueError even without prices, and so does choosing any price field without prices.
    """
    unknown = set(optional_price_fields) - OPTIONAL_PRICE_COLUMNS.keys()
    if unknown:
        raise ValueError(f"알 수 없는 가격 내보내기 열입니다: {sorted(unknown)!r}")
    if optional_price_fields and not include_price:
        raise ValueError(f"가격 열 없이 가격 내보내기 열을 고를 수 없습니다: {sorted(set(optional_price_fields))!r}")
    return [field for field in OPTIONAL_PRICE_COLUMNS if field in optional_price_fields]


def headers(optional_fields=(), include_price=False, optional_price_fields=()):
    price_fields = selected_price_fields(optional_price_fields, include_price)
    return ["카드명", "레어도", "수량", *(OPTIONAL_COLUMNS[field] for field in selected_fields(optional_fields)),
            *(PRICE_HEADERS if include_price else []), *(OPTIONAL_PRICE_COLUMNS[field] for field in price_fields)]


def price_values(card):
    """The full validated price record of one aggregated card: unit price, total, status, detail, edition, card number,
    product URL and observed time (KST); None (blank) wherever the status has no value. Only part of it is exported
    (see exported_price_values), but the app's final confirmation covers all of it."""
    price = card.get("price")
    if not isinstance(price, dict) or not PRICE_FIELDS <= set(price):
        raise ValueError(f"CID {card['cid']} 줄에 가격 정보가 없거나 올바르지 않습니다: {price!r}")
    unit = price["unit_price_krw"]
    if price["status"] in card_prices.PRICED_STATUSES:
        if isinstance(unit, bool) or not isinstance(unit, int) or unit <= 0:
            raise ValueError(f"CID {card['cid']} 가격 상태가 {price['status']}인데 단가가 올바르지 않습니다: {unit!r}")
        if not isinstance(price["observed_at"], datetime) or price["observed_at"].tzinfo is None:
            raise ValueError(f"CID {card['cid']} 가격의 관찰 시각이 올바르지 않습니다: {price['observed_at']!r}")
        amounts = [unit, unit * validate_quantity(card["quantity"])]
        observed_at = price["observed_at"].astimezone(KST).isoformat(timespec="seconds")
    else:
        if unit is not None:
            raise ValueError(f"CID {card['cid']} 가격 상태가 {price['status']!r}인데 단가 {unit!r}가 있습니다.")
        amounts = [None, None]
        observed_at = None
    return [*amounts, price["status"], price["detail"], price["locale"], price["card_number"], price["product_url"],
            observed_at]


def exported_price_values(card, price_fields):
    """Values of PRICE_HEADERS and the chosen price_fields (canonical order) for one aggregated card."""
    unit, total, status, _detail, _locale, card_number, product_url, observed_at = price_values(card)
    metadata = {"status": status, "product_url": product_url, "observed_at": observed_at, "card_number": card_number}
    return [unit, total, *(metadata[field] for field in price_fields)]


def row_values(card, optional_fields=(), include_price=False, optional_price_fields=()):
    price_fields = selected_price_fields(optional_price_fields, include_price)
    return [card["name"], rarities.RARITY_LABELS[card["rarity"]], card["quantity"],
            *(card[field] for field in selected_fields(optional_fields)),
            *(exported_price_values(card, price_fields) if include_price else [])]


HEADERS = headers(OPTIONAL_COLUMNS)


def _neutralize_formula(value):
    if isinstance(value, str) and value.startswith(FORMULA_PREFIXES):
        return "'" + value
    return value


def to_csv_bytes(cards, optional_fields=(), include_price=False, optional_price_fields=()):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(headers(optional_fields, include_price, optional_price_fields))
    for card in cards:
        writer.writerow([_neutralize_formula("" if v is None else v)
                         for v in row_values(card, optional_fields, include_price, optional_price_fields)])
    return buffer.getvalue().encode("utf-8-sig")


def to_xlsx_bytes(cards, optional_fields=(), include_price=False, optional_price_fields=()):
    header_row = headers(optional_fields, include_price, optional_price_fields)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "덱"
    sheet.append(header_row)
    for card in cards:
        sheet.append(row_values(card, optional_fields, include_price, optional_price_fields))
        # Force text cells so names like "=..." are stored as text, never as formulas.
        for cell in sheet[sheet.max_row]:
            if isinstance(cell.value, str):
                cell.data_type = "s"
    sheet.column_dimensions["A"].width = 36
    for name_column in ("한국어 이름", "일본어 이름", "영어 이름"):
        if name_column in header_row:
            sheet.column_dimensions[get_column_letter(header_row.index(name_column) + 1)].width = 30
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
