"""Observed TCGSHOP price of one official card (CID + rarity + physical edition).

A shop product is tied to a card only through verified official data:
1. fetch_official_prints() reads the card's official detail page for the edition (ja/ko) and returns every print
   with its printed card number and rarity rid. The page must be the requested CID and locale, show the local
   official name, and list exactly the (product, rarity) pairs of the local official SQLite snapshot.
2. get_card_price() looks up the stored shop observations (price_store) for the card numbers printed in the chosen
   rarity, and keeps only products whose exact shop rarity label maps to that one official rid at that number.
Shop product names are never used, there is no fuzzy matching, and rarity_prints.code is a rarity code, not a card
number. This module never contacts the shop.

The result covers the products seen in imported listing snapshots only: it is the lowest price among the observed
in-stock products, not the lowest price of the whole market.
"""

import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import catalog
import price_store
import rarities

LOCALES = ("ja", "ko")
OG_LOCALES = {"ja": "ja_JP", "ko": "ko_KR"}
REGIONS = {"ja": "JP", "ko": "KR"}
OG_LOCALE_PATTERN = re.compile(r'<meta property="og:locale" content="([^"]*)"')

# Exact TCGSHOP rarity labels -> the official rids that label can name. Only labels seen in stored listings are
# listed. A label naming several rids (the shop does not spell the colour variants) maps to a product only when
# exactly one of them is printed at that card number. Any other label ("UR OverFrame", "PSC OverFrame", ...) is
# unverified: the official database does not separate those finishes, so they are never coerced to a rarity.
SHOP_RARITY_LABELS = {
    "Normal": {1},
    "Secret Rare": {5, 43, 50},
    "Ultimate Rare": {6},
    "Collectors Rare": {16},
    "Prismatic Secret Rare": {36, 58},
}

# Status -> short Korean label shown in the app. Only "ok" carries a price.
STATUS_LABELS = {
    "ok": "수집 상품 최저가",
    "no_stock": "관찰 상품 모두 품절·재고 불명",
    "not_observed": "관찰된 상품 없음",
    "unverified": "레어도 대응 확인 불가 상품만 있음",
    "no_edition_print": "이 판본에 없는 레어도",
    "no_card_number": "조회 가능한 수록 번호 없음",
    "expired": "가격 만료 (관찰 후 12시간 경과)",
    "database_error": "가격 DB 오류",
    "cache_error": "가격 캐시 오류",
    "data_error": "가격 데이터 오류",
    "config_error": "가격 DB 설정 없음",
    "official_error": "공식 수록 정보 조회 실패",
}


class OfficialPrintError(Exception):
    pass


class PriceConfigError(price_store.PriceError):
    """The price database or cache is not configured for this app."""


def check_locale(locale):
    if locale not in LOCALES:
        raise ValueError(f"locale must be one of {LOCALES}, got {locale!r}")


def official_snapshot(cid, locale, database_path):
    """(official local name, {(pid, rid)}) of cid's prints in locale from the local official SQLite."""
    database_path = Path(database_path)
    if not database_path.is_file():
        raise OfficialPrintError(f"{database_path} does not exist")
    with closing(sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        row = connection.execute(f"SELECT name_{locale} FROM cards WHERE cid = ?", (cid,)).fetchone()
        prints = set(connection.execute("SELECT pid, rid FROM rarity_prints WHERE cid = ? AND locale = ?",
                                        (cid, locale)))
    if row is None:
        raise OfficialPrintError(f"CID {cid} is not in the official inventory snapshot")
    if row[0] is None:
        raise OfficialPrintError(f"CID {cid} is not listed in the {locale} official card list")
    return row[0], prints


def fetch_official_prints(cid, locale, database_path=rarities.DATABASE_PATH, session=None):
    """[{"pid", "card_number", "rid"}] of every official print of cid in locale, read from its detail page.

    Raises OfficialPrintError when the page cannot be fetched or is not exactly the requested card and locale,
    or when its prints differ from the local official snapshot.
    """
    if type(cid) is not int or cid <= 0:
        raise ValueError(f"CID must be a positive int, got {cid!r}")
    check_locale(locale)
    name, expected_prints = official_snapshot(cid, locale, database_path)
    url = catalog.detail_url(cid, locale)
    try:
        response = (session or catalog.new_session()).get(url, timeout=catalog.TIMEOUT_SECONDS)
        response.raise_for_status()
    except Exception as error:
        raise OfficialPrintError(f"{url}: {type(error).__name__}: {error}") from error
    if response.url != url:
        raise OfficialPrintError(f"{url} was redirected to {response.url}")
    html = response.text
    og_locales = OG_LOCALE_PATTERN.findall(html)
    if og_locales != [OG_LOCALES[locale]]:
        raise OfficialPrintError(f"{url}: page og:locale {og_locales} is not {OG_LOCALES[locale]}")
    try:
        _, image_src = catalog.parse_detail_page(html)
        prints = rarities.parse_detail_prints(html, locale, cid, name)
    except (catalog.CatalogError, rarities.RarityError) as error:
        raise OfficialPrintError(f"{url}: {error}") from error
    image_cids = re.findall(r"[?&]cid=(\d+)", image_src)
    if image_cids != [str(cid)]:
        raise OfficialPrintError(f"{url}: detail image {image_src!r} is not CID {cid}")
    found = {(entry["pid"], entry["rid"]) for entry in prints}
    if found != expected_prints:
        raise OfficialPrintError(
            f"{url}: official prints {sorted(found)} differ from the local snapshot {sorted(expected_prints)}; "
            "the official database changed, rebuild it with inventory.py and rarities.py")
    unknown = sorted({entry["rid"] for entry in prints} - set(rarities.RARITY_KEYS_BY_RID))
    if unknown:
        raise OfficialPrintError(f"{url}: rarity rids unknown to this app: {unknown}")
    return [{"pid": entry["pid"], "card_number": entry["card_number"], "rid": entry["rid"]} for entry in prints]


def price_result(status, locale, detail, **values):
    result = {"status": status, "detail": detail, "locale": locale, "unit_price_krw": None, "card_number": None,
              "product_id": None, "product_url": None, "observed_at": None, "expires_at": None,
              "matched_products": 0, "in_stock_products": 0, "excluded_unverified": 0}
    result.update(values)
    return result


def failure_status(error):
    if isinstance(error, PriceConfigError):
        return "config_error"
    if isinstance(error, price_store.StalePriceError):
        return "expired"
    if isinstance(error, price_store.PriceDatabaseError):
        return "database_error"
    if isinstance(error, price_store.PriceCacheError):
        return "cache_error"
    return "data_error"


def mapped_rid(product, official_rids_at_number):
    """The official rid a shop product is, or None when its label cannot be verified at that card number."""
    family = SHOP_RARITY_LABELS.get(product["rarity_label"], set())
    candidates = family & official_rids_at_number
    return next(iter(candidates)) if len(candidates) == 1 else None


def is_queryable(card_number, locale):
    match = price_store.CARD_NUMBER.fullmatch(card_number)
    return match is not None and match.group(1) == REGIONS[locale]


def observe_card_numbers(card_numbers, locale, database_url, redis_url):
    """{card number: {"observed": price_store.get_prices result} | {"not_found": True} | {"error": PriceError}}.

    Each number is queried once. After a PostgreSQL or Redis failure the remaining numbers are not queried and
    carry that same error: the backend is unavailable for this lookup.
    """
    check_locale(locale)
    if not database_url or not redis_url:
        missing = PriceConfigError("DATABASE_URL 또는 REDIS_URL 환경 변수가 설정되지 않았습니다.")
        return {number: {"error": missing} for number in card_numbers}
    observations = {}
    backend_error = None
    for number in sorted(set(card_numbers)):
        if backend_error is not None:
            observations[number] = {"error": backend_error}
            continue
        try:
            observations[number] = {"observed": price_store.get_prices(number, locale, database_url, redis_url)}
        except price_store.PriceNotFound:
            observations[number] = {"not_found": True}
        except price_store.PriceError as error:
            observations[number] = {"error": error}
            if isinstance(error, (price_store.PriceDatabaseError, price_store.PriceCacheError)):
                backend_error = error
    return observations


def queryable_numbers(official_prints, locale, rarity_keys):
    """Card numbers the given rarities of a card are printed under in locale that the price store can look up."""
    rids = {rarities.RARITIES[key][0] for key in rarity_keys}
    return sorted({entry["card_number"] for entry in official_prints
                   if entry["rid"] in rids and is_queryable(entry["card_number"], locale)})


def card_price(rarity, locale, official_prints, observations, redact_urls=()):
    """Lowest observed in-stock price of one rarity (a rarities.RARITIES key) of a card in edition locale.

    official_prints is fetch_official_prints(cid, locale); observations is observe_card_numbers() for (at least)
    queryable_numbers(official_prints, locale, [rarity]). Price problems are returned as a status (see
    STATUS_LABELS) with blank price fields; they are never raised. A failure for any of the rarity's card numbers
    is the result: the price is never the minimum of the remaining numbers.
    """
    if rarity not in rarities.RARITIES:
        raise ValueError(f"unknown rarity key {rarity!r}")
    check_locale(locale)
    rid = rarities.RARITIES[rarity][0]
    label = rarities.RARITY_LABELS[rarity]
    edition = {"ja": "일본판", "ko": "한국판"}[locale]
    numbers_of_rarity = {entry["card_number"] for entry in official_prints if entry["rid"] == rid}
    if not numbers_of_rarity:
        return price_result("no_edition_print", locale, f"공식 DB에 이 카드의 {edition} {label} 수록이 없습니다.")
    numbers = queryable_numbers(official_prints, locale, [rarity])
    skipped = len(numbers_of_rarity) - len(numbers)
    if not numbers:
        return price_result("no_card_number", locale,
                            f"{edition} {label} 수록 {skipped}건에 상점 조회에 쓸 수 있는 수록 번호가 없습니다.")

    matched, unverified, expiries = [], 0, []
    # Checked again here: a group that was fresh when queried may have expired before this result is built.
    now = datetime.now(timezone.utc)
    for number in numbers:
        observation = observations[number]
        if "error" in observation:
            error = observation["error"]
            message = price_store.redact(str(error), *redact_urls)
            return price_result(failure_status(error), locale, f"{number}: {type(error).__name__}: {message}")
        if "not_found" in observation:
            continue
        observed = observation["observed"]
        expires_at = min(datetime.fromisoformat(product["observed_at"]) for product in observed["prices"]) \
            + price_store.PRICE_LIFETIME
        if expires_at <= now:
            return price_result("expired", locale, f"{number}: 관찰 가격이 {expires_at.isoformat()}에 만료되었습니다 "
                                                   "(관찰 후 12시간 경과)")
        expiries.append(expires_at)
        official_rids = {entry["rid"] for entry in official_prints if entry["card_number"] == number}
        for product in observed["prices"]:
            product_rid = mapped_rid(product, official_rids)
            if product_rid is None:
                unverified += 1
            elif product_rid == rid:
                matched.append(product)

    notes = []
    if unverified:
        notes.append(f"레어도 대응을 확인할 수 없는 상품 {unverified}개 제외")
    if skipped:
        notes.append(f"수록 번호로 조회할 수 없는 수록 {skipped}건 제외")
    note = f" ({', '.join(notes)})" if notes else ""
    # Every queried group decides the result, so it is valid only until the first of them expires.
    counts = {"matched_products": len(matched), "excluded_unverified": unverified,
              "expires_at": min(expiries) if expiries else None}
    in_stock = [product for product in matched if product["stock_status"] == "in_stock"]
    if in_stock:
        best = min(in_stock, key=lambda p: (p["price_krw"], p["card_number"], int(p["product_id"])))
        return price_result(
            "ok", locale,
            f"관찰한 {edition} {label} 상품 {len(matched)}개 중 재고 있음 {len(in_stock)}개의 최저가"
            f" — 수집한 상품 기준이며 전체 시장 최저가가 아닙니다{note}",
            unit_price_krw=best["price_krw"], card_number=best["card_number"], product_id=best["product_id"],
            product_url=best["product_url"], observed_at=datetime.fromisoformat(best["observed_at"]),
            in_stock_products=len(in_stock), **counts)
    if matched:
        return price_result("no_stock", locale,
                            f"관찰한 {edition} {label} 상품 {len(matched)}개가 모두 품절이거나 재고를 확인할 수 없습니다{note}",
                            **counts)
    if unverified:
        return price_result("unverified", locale,
                            f"수록 번호 {', '.join(numbers)}에 관찰한 상품이 있지만 상점 레어도 표기가 {label}인지 "
                            f"확인할 수 없습니다{note}", **counts)
    return price_result("not_observed", locale, f"수록 번호 {', '.join(numbers)}의 {edition} {label} 상품을 "
                                                f"관찰한 적이 없습니다 (수집한 상품만 조회){note}", **counts)


def get_card_price(cid, rarity, locale, official_prints, database_url, redis_url):
    """card_price() for one CID and rarity, querying the price store for its card numbers."""
    if type(cid) is not int or cid <= 0:
        raise ValueError(f"CID must be a positive int, got {cid!r}")
    if rarity not in rarities.RARITIES:
        raise ValueError(f"unknown rarity key {rarity!r}")
    check_locale(locale)
    numbers = queryable_numbers(official_prints, locale, [rarity])
    observations = observe_card_numbers(numbers, locale, database_url, redis_url)
    return card_price(rarity, locale, official_prints, observations, (database_url, redis_url))
