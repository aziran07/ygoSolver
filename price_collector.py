"""On-demand TCGSHOP price collection for one exact official card number (the only module that contacts the shop).

Route: the listing category search of the stored listing page (form sortForm, GET goods_list.php with the
category Index and searchstring; Index 288 = Japanese cards, 276 = Korean cards). robots.txt allows goods_list.php
and disallows the rest of the site, so search_result.php is never used. This search route and its result markers
were read from the stored listing page and are NOT verified against a live search response yet.

Every request first reserves the global request gate in PostgreSQL (price_store.reserve_collection): TCGSHOP
robots.txt declares `Crawl-delay: 43200`, so the whole app sends at most one shop request per 12 hours, and a
failed request uses the interval too. One request reads only the first result page; it is not the whole catalog.
There is no retry, no sleeping and no background schedule: a later page load tries again once the gate allows.
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
USER_AGENT = "ygoSolver price lookup (one request per 12 hours, robots.txt Crawl-delay)"


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
    echoes the requested card number, and every listed product must have exactly that card number. A page without
    product blocks fails: no verified "no results" marker is known, so it is not treated as an empty result.
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


def collect_card_number(card_number, locale, database_url, fetching=nullcontext, session=None):
    """Collect one card number's search page if the global gate allows it now, and store it.

    Returns {"state": "stored", "snapshot_id", "product_count", "observed_at"},
    {"state": "wait", "next_allowed_at", "last_attempt"} (no request sent; last_attempt is
    price_store.latest_attempt for this number), or
    {"state": "failed", "error", "attempted_at", "next_allowed_at"} (the request was sent or reserved and failed;
    recorded in PostgreSQL). fetching(card_number) is a context manager around the HTTP request only (the app's
    spinner). PostgreSQL failures raise price_store.PriceError; when the reservation fails no request is sent.
    """
    url = search_url(card_number, locale)
    reservation = price_store.reserve_collection(card_number, locale, url, database_url)
    if not reservation["allowed"]:
        return {"state": "wait", "next_allowed_at": reservation["next_allowed_at"],
                "last_attempt": price_store.latest_attempt(card_number, locale, database_url)}
    try:
        with fetching(card_number):
            metadata, products = fetch_search_page(card_number, locale, session)
    except CollectionError as error:
        message = str(error)
        price_store.finish_attempt(reservation["attempt_id"], message, database_url)
        return {"state": "failed", "error": message, "attempted_at": reservation["reserved_at"],
                "next_allowed_at": reservation["next_allowed_at"]}
    try:
        stored = price_store.store_listing(metadata, products, database_url, reservation["attempt_id"],
                                           SEARCH_SCOPE)
    except price_store.PriceError as error:
        # The response was received but not stored: the attempt is recorded as failed and the error propagates.
        price_store.finish_attempt(reservation["attempt_id"], f"응답 저장 실패: {error}", database_url)
        raise
    return {"state": "stored", "snapshot_id": stored["snapshot_id"], "product_count": stored["product_count"],
            "observed_at": datetime.fromisoformat(metadata["observed_at"])}
