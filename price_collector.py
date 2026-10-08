"""On-demand TCGSHOP price collection for one exact official card number (the only module that contacts the shop).

Route: the listing category search (form sortForm; its search button runs goodsSort(), which opens
goods_list.php?data=&Index=<category>&searchstring=<text>; Index 288 = Japanese cards, 276 = Korean cards).
robots.txt allows goods_list.php and disallows the rest of the site, so search_result.php is never used. Verified
with real Japanese search responses on 2026-10-08 (data/experiments/on_demand_prices, not in Git): the page echoes
the search text in sortForm, lists only products of that card number between the "상품 목록 시작/끝" comments,
and an empty result leaves only a spacer row there. Korean (Index 276) search responses were not observed.

The request is sent immediately when a selected card's stored price is missing or expired (stored prices stay
valid for 12 hours, PRICE_LIFETIME). There is no global request gate or interval between requests: the site's
robots.txt `Crawl-delay: 43200` is not applied to these single-card lookups. Every request is recorded in
PostgreSQL (price_store.begin_collection) before it is sent. One request reads only the first result page; it is
not the whole catalog. There is no automatic retry and no background schedule: a failed request stays the card
number's result until an explicit retry (retry=True), and a verified empty search is reused for 12 hours, so reruns
do not send the same request again.
"""

import hashlib
from contextlib import nullcontext
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

import prices
import price_store

SEARCH_URL = prices.LIST_URL_PREFIX + "?data=&Index={index}&searchstring={card_number}"
CATEGORY_INDEXES = {locale: index for index, locale in prices.CATEGORY_LOCALES.items()}
SEARCH_SCOPE = "card_number_search_first_page"
TIMEOUT_SECONDS = (5, 20)  # connect, read
USER_AGENT = "ygoSolver price lookup (single card number search)"
LIST_START = "<!-- 상품 목록 시작 -->"
LIST_END = "<!-- 상품 목록 끝 -->"
# Whitespace-normalized content between LIST_START and LIST_END of a real search page without results.
EMPTY_LIST = '<tr> <tr><td height="10"></td></tr> </form> </table></tr>'


class CollectionError(prices.PriceError):
    """The shop request failed or its response is not exactly the requested search result."""


def search_url(card_number, locale):
    match = price_store.CARD_NUMBER.fullmatch(card_number) if isinstance(card_number, str) else None
    if match is None or prices.REGION_LOCALES[match.group(1)] != locale:
        raise ValueError(f"{card_number!r} is not a {locale} card number")
    return SEARCH_URL.format(index=CATEGORY_INDEXES[locale], card_number=card_number)


def parse_search_page(html, card_number, locale):
    """Products of a search result page, only when it is exactly the requested category search.

    The page must carry one sortForm for goods_list.php whose category Index is the locale's and whose search box
    echoes the requested card number, and every listed product must have exactly that card number. [] means a
    verified empty result: no product block, and the one product list between LIST_START and LIST_END holds exactly
    the spacer of a real empty search page. Any other page without product blocks fails.
    """
    url = search_url(card_number, locale)
    soup = BeautifulSoup(html, "html.parser")
    forms = soup.select('form[name="sortForm"]')
    if len(forms) != 1 or forms[0].get("action") != "goods_list.php":
        raise CollectionError(f"{url}: expected one sortForm for goods_list.php, found {len(forms)}")
    form_indexes = [field.get("value") for field in forms[0].select('input[name="Index"]')]
    if form_indexes != [CATEGORY_INDEXES[locale]]:
        raise CollectionError(f"{url}: page category {form_indexes} is not Index {CATEGORY_INDEXES[locale]}")
    searched = [field.get("value") for field in forms[0].select('input[name="searchstring"]')]
    if searched != [card_number]:
        raise CollectionError(f"{url}: page search box shows {searched}, not the requested {card_number}")
    if not soup.select("table[id^=list_card_]"):
        if html.count(LIST_START) != 1 or html.count(LIST_END) != 1 or html.index(LIST_START) > html.index(LIST_END):
            raise CollectionError(f"{url}: no product blocks and no single product list between {LIST_START} and "
                                  f"{LIST_END}")
        product_list = " ".join(html[html.index(LIST_START) + len(LIST_START):html.index(LIST_END)].split())
        if product_list != EMPTY_LIST:
            raise CollectionError(f"{url}: no product blocks, and the product list is not the known empty result: "
                                  f"{product_list[:200]!r}")
        return []
    try:
        products = prices.parse_tcgshop_list(html, url)
    except prices.PriceError as error:
        raise CollectionError(f"{url}: {error}") from error
    off_target = sorted({product["card_number"] for product in products} - {card_number})
    if off_target:
        raise CollectionError(f"{url}: search result lists other card numbers {off_target}")
    return products


def fetch_search_page(card_number, locale, session=None):
    """(metadata, products) of one search request. Exactly one GET: no redirects followed, no retry."""
    url = search_url(card_number, locale)
    try:
        # An injected session is used as is; otherwise a one-off request without a session to close.
        response = (session or requests).get(url, timeout=TIMEOUT_SECONDS, allow_redirects=False,
                                             headers={"User-Agent": USER_AGENT})
    except requests.RequestException as error:
        raise CollectionError(f"{url}: {type(error).__name__}: {error}") from error
    observed_at = datetime.now(timezone.utc)
    if response.status_code != 200:
        detail = ""
        if response.is_redirect:
            detail = f" redirect to {response.headers.get('Location')!r}"
        elif response.status_code == 429:
            detail = f" Retry-After {response.headers.get('Retry-After')!r}"
        raise CollectionError(f"{url}: HTTP {response.status_code}{detail}")
    content_type = response.headers.get("Content-Type", "")
    if not content_type.startswith("text/html"):
        raise CollectionError(f"{url}: content type {content_type!r} is not text/html")
    body = response.content
    if not body:
        raise CollectionError(f"{url}: empty response body")
    try:
        html = body.decode("euc-kr")
    except UnicodeDecodeError as error:
        raise CollectionError(f"{url}: response is not valid EUC-KR: {error}") from error
    products = parse_search_page(html, card_number, locale)
    metadata = {"url": url, "observed_at": observed_at.isoformat(), "status_code": 200,
                "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "content_type": content_type}
    return metadata, products


def collect_card_number(card_number, locale, database_url, fetching=nullcontext, session=None, retry=False):
    """Collect one card number's search page now and store it.

    Returns {"state": "stored", "snapshot_id", "product_count", "observed_at"},
    {"state": "empty", "observed_at"} (verified empty search; observed_at is when it was recorded), or
    {"state": "failed", "error", "attempted_at"}. Every request is recorded in PostgreSQL. Unless retry is true, the
    card number's latest recorded attempt is the result without a request when it failed (at any age) or was an
    empty search less than PRICE_LIFETIME ago. fetching(card_number) is a context manager around the HTTP request
    only (the app's spinner). PostgreSQL failures raise price_store.PriceError; when the attempt cannot be recorded
    no request is sent.
    """
    url = search_url(card_number, locale)
    if not retry:
        last = price_store.latest_attempt(card_number, locale, database_url)
        if last is not None and last["outcome"] == "failed":
            return {"state": "failed", "error": last["error"], "attempted_at": last["reserved_at"]}
        if last is not None and last["outcome"] == "empty" \
                and last["finished_at"] + price_store.PRICE_LIFETIME > datetime.now(timezone.utc):
            return {"state": "empty", "observed_at": last["finished_at"]}
    attempt = price_store.begin_collection(card_number, locale, url, database_url)
    try:
        with fetching(card_number):
            metadata, products = fetch_search_page(card_number, locale, session)
    except CollectionError as error:
        message = str(error)
        price_store.finish_attempt(attempt["attempt_id"], message, database_url)
        return {"state": "failed", "error": message, "attempted_at": attempt["started_at"]}
    if not products:
        return {"state": "empty", "observed_at": price_store.record_empty(attempt["attempt_id"], database_url)}
    try:
        stored = price_store.store_listing(metadata, products, database_url, attempt["attempt_id"], SEARCH_SCOPE)
    except price_store.PriceError as error:
        # The response was received but not stored: the attempt is recorded as failed and the error propagates.
        price_store.finish_attempt(attempt["attempt_id"], f"응답 저장 실패: {error}", database_url)
        raise
    return {"state": "stored", "snapshot_id": stored["snapshot_id"], "product_count": stored["product_count"],
            "observed_at": datetime.fromisoformat(metadata["observed_at"])}
