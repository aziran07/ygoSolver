"""Observed TCGSHOP price of one official card (CID + rarity + physical edition).

A shop product is tied to a card only through verified official data:
1. fetch_official_prints() reads the card's official detail page for the edition (ja/ko) and returns every print
   with its printed card number and rarity rid. The page must be the requested CID and locale, show the local
   official name, and list exactly the (product, rarity) pairs of the local official SQLite snapshot.
2. get_card_price() looks up the stored shop observations (price_store) for the card numbers printed in the chosen
   rarity, and keeps only products whose exact shop rarity label maps to that one official rid at that number.
Shop product names are never used, there is no fuzzy matching, and rarity_prints.code is a rarity code, not a card
number. card_price() never contacts the shop.

price_with_collection() adds on-demand collection for one selected rarity: only when its stored price is missing
or expired, it asks price_collector to fetch the search page of each missing card number in turn, stores it, reads
it back from PostgreSQL/Redis and maps it with the same strict rules.

The result covers the stored products only (listing snapshots and first search result pages): it is the lowest
price among the observed in-stock products, not the lowest price of the whole market.
"""

import re
import sqlite3
from contextlib import closing, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path

import catalog
import price_collector
import price_store
import rarities

LOCALES = ("ja", "ko")
OG_LOCALES = {"ja": "ja_JP", "ko": "ko_KR"}
REGIONS = {"ja": "JP", "ko": "KR"}
KST = timezone(timedelta(hours=9), "KST")
# Statuses for which the selected rarity's missing or expired card numbers are collected from the shop.
COLLECTABLE_STATUSES = ("not_observed", "expired")
OG_LOCALE_PATTERN = re.compile(r'<meta property="og:locale" content="([^"]*)"')

# Exact TCGSHOP rarity labels -> the official rids that label can name. Only labels seen on stored products or in the
# rarity filter (select name="Rare") of the stored listing page are listed. A label naming several rids (the shop does
# not spell the colour variants) maps to a product only when exactly one of them is printed at that card number. Any
# other label ("UR OverFrame", "PSC OverFrame", ...) is unverified: the official database does not separate those
# finishes, so they are never coerced to a rarity.
SHOP_RARITY_LABELS = {
    "Normal": {1},
    "Rare": {2},
    "Super Rare": {3},
    "Ultra Rare": {4},
    "Secret Rare": {5, 43, 50},
    "Ultimate Rare": {6},
    "Collectors Rare": {16},
    "Prismatic Secret Rare": {36, 58},
}

# Status -> short Korean label shown in the app's price field; the result's "detail" explains it. Only "ok" carries
# a price. Exports keep the status keys, not these labels.
STATUS_LABELS = {
    "ok": "수집 상품 최저가",
    "no_stock": "품절·재고 불명",
    "not_observed": "가격 미수집",
    "unverified": "레어도 확인 불가",
    "no_edition_print": "이 판본에 없음",
    "no_card_number": "수록 번호 없음",
    "expired": "가격 만료",
    "database_error": "가격 DB 오류",
    "cache_error": "가격 캐시 오류",
    "data_error": "가격 데이터 오류",
    "config_error": "가격 DB 설정 없음",
    "official_error": "공식 수록 조회 실패",
    "not_listed": "상점 상품 없음",
    "collection_failed": "수집 실패",
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
    return price_result("not_observed", locale, f"수록 번호 {', '.join(numbers)}의 {edition} {label} 상품 가격이 가격 DB에 "
                                                f"저장되어 있지 않습니다. 품절이나 0원이 아니라 수집한 상품(목록 페이지·카드 번호 "
                                                f"검색 결과 첫 페이지) 중에 이 레어도가 없다는 뜻입니다{note}", **counts)


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


def kst_text(moment):
    return moment.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")


def missing_reason(observation, now):
    """Why a card number's stored price cannot be used ("never collected" / "expired"), or None if it can."""
    if "not_found" in observation:
        return "저장된 가격 없음"
    if "error" in observation:
        return "저장된 가격 만료" if isinstance(observation["error"], price_store.StalePriceError) else None
    observed_times = [datetime.fromisoformat(product["observed_at"]) for product in observation["observed"]["prices"]]
    return "저장된 가격 만료" if min(observed_times) + price_store.PRICE_LIFETIME <= now else None


def numbers_to_collect(rarity, locale, official_prints, observations):
    """Queryable card numbers of the rarity whose stored price is missing or expired: expired ones first (an expired
    group keeps invalidating the whole result until it is refreshed), then never collected ones, each by number."""
    now = datetime.now(timezone.utc)
    reasons = {number: missing_reason(observations[number], now)
               for number in queryable_numbers(official_prints, locale, [rarity])}
    return sorted((number for number, reason in reasons.items() if reason is not None),
                  key=lambda number: (reasons[number] != "저장된 가격 만료", number))


def price_with_collection(rarity, locale, official_prints, observations, database_url, redis_url,
                          collect=None, fetching=nullcontext, retry=False):
    """card_price() of the selected rarity, collecting its missing or expired card numbers from the shop first.

    Collection runs only when the stored result is not_observed or expired, one card number at a time through
    collect (default price_collector.collect_card_number, called with retry); a stored page is read back with
    observe_card_numbers (which updates observations in place) and mapped by card_price again. The first failed
    number stops collection and is the result (collection_failed). A number whose search was verified empty replaces
    only its own observation (updated in place) with "not found", so an expired stored price of that number is never
    used; when the result is then not_observed, it is not_listed. Database, cache, data, config and official errors and every
    other status never contact the shop. The detail depends only on stored data and attempts, never on whether this
    run collected, so a rerun right after a collection keeps the same exported values (and the user's confirmation).
    """
    collect = collect or price_collector.collect_card_number  # looked up per call so it can be replaced in tests
    redact_urls = (database_url, redis_url)
    result = card_price(rarity, locale, official_prints, observations, redact_urls)
    collected = []
    empty_searches = {}  # card number -> when its search was verified empty
    while result["status"] in COLLECTABLE_STATUSES:
        pending = [number for number in numbers_to_collect(rarity, locale, official_prints, observations)
                   if number not in collected]
        if not pending:
            break
        number = pending[0]
        reason = missing_reason(observations[number], datetime.now(timezone.utc))
        try:
            outcome = collect(number, locale, database_url, fetching, retry=retry)
        except price_store.PriceError as error:
            message = price_store.redact(str(error), *redact_urls)
            return price_result(failure_status(error), locale,
                                f"{number} 가격 수집 중 오류: {type(error).__name__}: {message}")
        if outcome["state"] == "failed":
            return price_result("collection_failed", locale,
                                f"{number}: {reason}. {kst_text(outcome['attempted_at'])} 상점 수집 실패 — "
                                f"{outcome['error']}. '가격 수집 다시 시도'를 누르면 다시 요청합니다")
        collected.append(number)
        if outcome["state"] == "empty":
            empty_searches[number] = outcome["observed_at"]
            # The shop no longer lists this number: its stored (possibly expired) rows are not current, so only
            # this number is replaced by "nothing observed" and the result is mapped again by the same rules.
            observations[number] = {"not_found": True}
            result = card_price(rarity, locale, official_prints, observations, redact_urls)
            continue
        if outcome["state"] != "stored":
            raise ValueError(f"{number}: unknown collection outcome state {outcome['state']!r}")
        observations.update(observe_card_numbers([number], locale, database_url, redis_url))
        result = card_price(rarity, locale, official_prints, observations, redact_urls)
    if result["status"] == "not_observed" and empty_searches:
        searched = ", ".join(f"{number}({kst_text(moment)})" for number, moment in sorted(empty_searches.items()))
        return price_result("not_listed", locale,
                            f"TCGSHOP 카드 번호 검색 결과에 상품이 없습니다: {searched}. 검색 결과는 12시간 동안 다시 쓰며, "
                            f"'가격 수집 다시 시도'를 누르면 다시 검색합니다. {result['detail']}",
                            expires_at=min(empty_searches.values()) + price_store.PRICE_LIFETIME)
    return result
