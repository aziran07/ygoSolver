"""PostgreSQL storage for full shop catalog collection runs (cardmoa, tcgshop).

A run collects every listing page of one shop category. Each stored page keeps the gzipped response bytes,
their SHA-256 and the parsed result; each product keeps its full parsed dict as JSONB plus queryable columns.
These shop_catalog_* tables are separate from the price_* tables of price_store.py, which are not touched.

Run lifecycle:
- start_run creates a 'running' run. The first stored page fixes total_products and total_pages; every later
  page must report the same totals.
- store_page stores one page, its products and the run totals in one transaction. Replaying an identical page
  (same URL, SHA-256, observed_at and parsed result) is a no-op; a different replay is rejected.
- fail_run records an error, also appended to shop_catalog_failures. Committed pages are kept, and the run
  resumes by storing further pages: the first valid new page returns it to 'running' and clears the run's
  current error, while every past failure stays in shop_catalog_failures. reopen_run explicitly returns a
  failed run to 'running' (e.g. when it failed at completion with all pages stored), keeping that history.
- complete_run checks in SQL that pages 1..total_pages are all stored and that the run holds exactly
  total_products distinct products. Otherwise it raises and the run stays unchanged. A completed run is never
  modified again.

An empty catalog is stored as total_products=0, total_pages=1 with one page that has no products.

View shop_catalog_lowest_prices (created by init_db; it only reads the tables above):
- Source: for each (shop, category) only its newest run (highest id), and only when that run is completed. A newer
  running or failed run hides the category; no older completed run is used instead.
- Key: locale + printed card number + rarity. The card number is the shop's printed code with only a trailing
  condition grade removed ("A급", "(A급)", " (A급)", likewise B/C, the forms the shops print); other markers such as
  "(신)" or "(마크)" stay part of the code. It is not mapped to an official CID.
- Rarity: rarity_labels lists the exact shop labels. 'verified_alias' labels name one official finish and get its
  official Korean label (rarities.py), so both shops share the group. 'exact_shop_label' labels name one finish only
  that shop uses and stay "<shop>:<label>". Any other label is 'unverified_finish' ("<shop>:<label>"): it may cover
  several finishes (TCGSHOP "Secret Rare" also lists Special Red versions), so each such product is its own row.
- Rows without locale, card number, rarity label or numeric price are left out; condition and stock are ignored, so
  a sold-out product can be the minimum. Ties go to the lower shop name, then the lower numeric product_id.
"""

import gzip
import hashlib
import json
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import psycopg
from psycopg.types.json import Jsonb

from prices import PriceError
from price_store import PriceDatabaseError, connect_postgres, redact

SHOP_HOSTS = {
    "cardmoa": {"cardmoa.com", "www.cardmoa.com"},
    "tcgshop": {"www.tcgshop.co.kr", "tcgshop.co.kr"},
}
LOCALES = ("ko", "ja", None)
STOCK_STATUSES = ("in_stock", "out_of_stock", "unknown")
PRODUCT_ID = re.compile(r"[0-9]+")
REQUIRED_PRODUCT_KEYS = {"product_id", "name", "product_url", "locale", "price_krw", "list_price_krw",
                         "stock_status", "raw"}
OPTIONAL_PRODUCT_KEYS = {"card_number", "rarity_label"}
RUN_FIELDS = ("id", "shop", "category", "locale", "source_url", "status", "started_at", "updated_at",
              "completed_at", "failed_at", "error", "total_products", "total_pages")

SCHEMA = """
CREATE TABLE IF NOT EXISTS shop_catalog_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    shop TEXT NOT NULL CHECK (shop IN ('cardmoa', 'tcgshop')),
    category TEXT NOT NULL CHECK (category <> ''),
    locale TEXT CHECK (locale IN ('ko', 'ja')),
    source_url TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'failed', 'completed')),
    started_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    failed_at TIMESTAMPTZ,
    error TEXT,
    total_products INTEGER CHECK (total_products >= 0),
    total_pages INTEGER CHECK (total_pages > 0),
    CHECK ((status = 'completed') = (completed_at IS NOT NULL)),
    CHECK ((status = 'failed') = (failed_at IS NOT NULL)),
    CHECK ((status = 'failed') = (error IS NOT NULL AND error <> '')),
    CHECK ((total_products IS NULL) = (total_pages IS NULL)),
    CHECK (total_products <> 0 OR total_pages = 1),
    CHECK (status <> 'completed' OR total_pages IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS shop_catalog_pages (
    run_id BIGINT NOT NULL REFERENCES shop_catalog_runs(id),
    page_number INTEGER NOT NULL CHECK (page_number > 0),
    source_url TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    bytes INTEGER NOT NULL CHECK (bytes > 0),
    body_gzip BYTEA NOT NULL,
    parsed JSONB NOT NULL,
    stored_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (run_id, page_number)
);
CREATE TABLE IF NOT EXISTS shop_catalog_products (
    run_id BIGINT NOT NULL,
    page_number INTEGER NOT NULL,
    product_id TEXT NOT NULL CHECK (product_id ~ '^[0-9]+$'),
    name TEXT NOT NULL,
    product_url TEXT NOT NULL,
    locale TEXT CHECK (locale IN ('ko', 'ja')),
    card_number TEXT,
    rarity_label TEXT,
    price_krw INTEGER CHECK (price_krw >= 0),
    list_price_krw INTEGER CHECK (list_price_krw >= 0),
    stock_status TEXT NOT NULL CHECK (stock_status IN ('in_stock', 'out_of_stock', 'unknown')),
    product JSONB NOT NULL,
    PRIMARY KEY (run_id, product_id),
    FOREIGN KEY (run_id, page_number) REFERENCES shop_catalog_pages(run_id, page_number)
);
CREATE TABLE IF NOT EXISTS shop_catalog_failures (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES shop_catalog_runs(id),
    failed_at TIMESTAMPTZ NOT NULL,
    error TEXT NOT NULL CHECK (error <> '')
);
CREATE OR REPLACE VIEW shop_catalog_lowest_prices AS
WITH latest_runs AS (
    SELECT DISTINCT ON (shop, category) id, shop, status
    FROM shop_catalog_runs
    ORDER BY shop, category, id DESC
),
rarity_labels (shop, shop_label, rarity_label, rarity_mapping_status) AS (
    VALUES
    ('cardmoa', '노멀', '노멀', 'verified_alias'),
    ('cardmoa', '레어', '레어', 'verified_alias'),
    ('cardmoa', '슈퍼레어', '슈퍼 레어', 'verified_alias'),
    ('cardmoa', '울트라레어', '울트라 레어', 'verified_alias'),
    ('cardmoa', '얼티미트레어', '얼티미트 레어', 'verified_alias'),
    ('cardmoa', '홀로그래픽레어', '홀로그래픽 레어', 'verified_alias'),
    ('cardmoa', '씨크릿레어', '시크릿 레어', 'verified_alias'),
    ('cardmoa', '엑스트라 씨크릿레어', '엑스트라 시크릿 레어', 'verified_alias'),
    ('tcgshop', 'Normal', '노멀', 'verified_alias'),
    ('tcgshop', 'Rare', '레어', 'verified_alias'),
    ('tcgshop', 'Super Rare', '슈퍼 레어', 'verified_alias'),
    ('tcgshop', 'Ultra Rare', '울트라 레어', 'verified_alias'),
    ('tcgshop', 'Ultimate Rare', '얼티미트 레어', 'verified_alias'),
    ('tcgshop', 'Holographic Rare', '홀로그래픽 레어', 'verified_alias'),
    ('tcgshop', 'Collectors Rare', '컬렉터즈 레어', 'verified_alias'),
    ('tcgshop', 'Extra Secret Rare', '엑스트라 시크릿 레어', 'verified_alias'),
    ('cardmoa', '시크릿스페셜레드버젼', 'cardmoa:시크릿스페셜레드버젼', 'exact_shop_label'),
    ('cardmoa', 'DT-노멀', 'cardmoa:DT-노멀', 'exact_shop_label'),
    ('tcgshop', 'UR OverFrame', 'tcgshop:UR OverFrame', 'exact_shop_label'),
    ('tcgshop', 'PSC OverFrame', 'tcgshop:PSC OverFrame', 'exact_shop_label'),
    ('tcgshop', 'KC Rare', 'tcgshop:KC Rare', 'exact_shop_label')
),
candidates AS (
    SELECT p.locale,
           regexp_replace(p.card_number, '( ?[(][ABC]급[)]|[ABC]급)$', '') AS card_number,
           coalesce(l.rarity_label, r.shop || ':' || p.rarity_label) AS rarity_label,
           coalesce(l.rarity_mapping_status, 'unverified_finish') AS rarity_mapping_status,
           p.price_krw, r.shop, p.product_id, p.product_url, p.run_id, p.page_number, g.observed_at
    FROM latest_runs r
    JOIN shop_catalog_products p ON p.run_id = r.id
    JOIN shop_catalog_pages g ON g.run_id = p.run_id AND g.page_number = p.page_number
    LEFT JOIN rarity_labels l ON l.shop = r.shop AND l.shop_label = p.rarity_label
    WHERE r.status = 'completed'
      AND p.locale IS NOT NULL AND p.card_number IS NOT NULL AND p.rarity_label IS NOT NULL
      AND p.price_krw IS NOT NULL
)
SELECT DISTINCT ON (locale, card_number, rarity_label,
                    CASE WHEN rarity_mapping_status = 'unverified_finish' THEN shop || ':' || product_id END)
       locale, card_number, rarity_label, rarity_mapping_status, price_krw, shop, product_id, product_url,
       run_id, page_number, observed_at
FROM candidates
ORDER BY locale, card_number, rarity_label,
         CASE WHEN rarity_mapping_status = 'unverified_finish' THEN shop || ':' || product_id END,
         price_krw, shop, product_id::numeric, run_id;
"""


class ShopCatalogError(PriceError):
    """Invalid catalog input or a run state that does not allow the requested change."""


@contextmanager
def transaction(database_url, action):
    """One committed transaction; psycopg errors become PriceDatabaseError with passwords redacted."""
    connection = connect_postgres(database_url)
    try:
        with connection:
            yield connection
    except psycopg.Error as error:
        raise PriceDatabaseError(f"PostgreSQL {action} failed: {redact(str(error), database_url)}") from error
    finally:
        connection.close()


def check_positive_int(value, label):
    if type(value) is not int or value <= 0:
        raise ShopCatalogError(f"{label} must be a positive integer, got {value!r}")


def check_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ShopCatalogError(f"{label} must be a nonempty string, got {value!r}")


def check_shop_url(url, shop, label):
    check_text(url, label)
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as error:
        raise ShopCatalogError(f"{label} is not a valid URL: {url!r} ({error})") from error
    if parts.scheme not in ("http", "https"):
        raise ShopCatalogError(f"{label} must use http or https: {url!r}")
    if parts.username is not None or parts.password is not None or port is not None:
        raise ShopCatalogError(f"{label} must not contain credentials or a port: {url!r}")
    if parts.hostname not in SHOP_HOSTS[shop]:
        raise ShopCatalogError(f"{label} host {parts.hostname!r} does not belong to shop {shop!r}")


def check_observed_at(observed_at):
    if not isinstance(observed_at, datetime) or observed_at.utcoffset() != timedelta(0):
        raise ShopCatalogError(f"observed_at must be a UTC-aware datetime, got {observed_at!r}")
    if observed_at > datetime.now(timezone.utc):
        raise ShopCatalogError(f"observed_at {observed_at.isoformat()} is in the future")


def check_price(product, field, none_allowed_when):
    price = product[field]
    raw = product["raw"]
    if price is None:
        if not none_allowed_when(raw):
            raise ShopCatalogError(f"product {product['product_id']}: {field} is None without source evidence")
        return
    if type(price) is not int or price < 0:
        raise ShopCatalogError(f"product {product['product_id']}: {field} must be a nonnegative int, got {price!r}")
    if price == 0 and not (isinstance(raw.get("price_zero_evidence"), str) and raw["price_zero_evidence"].strip()):
        raise ShopCatalogError(f"product {product['product_id']}: {field} is 0 without raw.price_zero_evidence")


def check_product(product, shop):
    if not isinstance(product, dict):
        raise ShopCatalogError(f"product must be a dict, got {product!r}")
    missing = REQUIRED_PRODUCT_KEYS - product.keys()
    unknown = product.keys() - REQUIRED_PRODUCT_KEYS - OPTIONAL_PRODUCT_KEYS
    if missing or unknown:
        raise ShopCatalogError(f"product keys invalid: missing {sorted(missing)}, unknown {sorted(unknown)}")
    product_id = product["product_id"]
    if not isinstance(product_id, str) or not PRODUCT_ID.fullmatch(product_id) or int(product_id) == 0:
        raise ShopCatalogError(f"product_id must be a positive digit string, got {product_id!r}")
    raw = product["raw"]
    if not isinstance(raw, dict):
        raise ShopCatalogError(f"product {product_id}: raw must be a dict")
    check_text(product["name"], f"product {product_id} name")
    check_shop_url(product["product_url"], shop, f"product {product_id} product_url")
    if product["locale"] not in LOCALES:
        raise ShopCatalogError(f"product {product_id}: locale must be 'ko', 'ja' or None")
    check_text(raw.get("locale_evidence"), f"product {product_id} raw.locale_evidence")
    if product["stock_status"] not in STOCK_STATUSES:
        raise ShopCatalogError(f"product {product_id}: stock_status must be one of {STOCK_STATUSES}")
    for field in OPTIONAL_PRODUCT_KEYS & product.keys():
        check_text(product[field], f"product {product_id} {field}")
    check_price(product, "price_krw",
                lambda raw: isinstance(raw.get("price_error"), str) and bool(raw["price_error"].strip()))
    # A missing separate list price is source data, not an error: the source shows none (raw.list_price_text null).
    check_price(product, "list_price_krw", lambda raw: "list_price_text" in raw and raw["list_price_text"] is None)


def check_parsed(parsed, page_number, shop):
    if not isinstance(parsed, dict) or parsed.keys() != {"products", "total_products", "total_pages"}:
        raise ShopCatalogError("parsed must be a dict with exactly products, total_products and total_pages")
    products, total_products, total_pages = parsed["products"], parsed["total_products"], parsed["total_pages"]
    if not isinstance(products, list):
        raise ShopCatalogError("parsed products must be a list")
    if type(total_products) is not int or total_products < 0:
        raise ShopCatalogError(f"total_products must be a nonnegative int, got {total_products!r}")
    check_positive_int(total_pages, "total_pages")
    if total_products == 0:
        if total_pages != 1 or products:
            raise ShopCatalogError("an empty catalog must be total_pages=1 with no products")
    elif not products:
        raise ShopCatalogError(f"page {page_number} has no products but total_products is {total_products}")
    if page_number > total_pages:
        raise ShopCatalogError(f"page {page_number} exceeds total_pages {total_pages}")
    if len(products) > total_products:
        raise ShopCatalogError(f"page {page_number} has {len(products)} products, more than {total_products}")
    for product in products:
        check_product(product, shop)
    ids = [product["product_id"] for product in products]
    if len(set(ids)) != len(ids):
        raise ShopCatalogError(f"page {page_number} contains duplicate product IDs")
    try:
        json.dumps(parsed, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ShopCatalogError(f"parsed is not JSON serializable: {error}") from error


def lock_run(connection, run_id):
    row = connection.execute(
        "SELECT shop, status, total_products, total_pages FROM shop_catalog_runs WHERE id = %s FOR UPDATE",
        (run_id,),
    ).fetchone()
    if row is None:
        raise ShopCatalogError(f"catalog run {run_id} does not exist")
    return row


def init_db(database_url):
    """Create the catalog tables in one transaction (safe to repeat)."""
    with transaction(database_url, "catalog schema creation") as connection:
        connection.execute(SCHEMA)


def start_run(database_url, shop, category, locale, source_url):
    """Create a running catalog run and return its ID."""
    if shop not in SHOP_HOSTS:
        raise ShopCatalogError(f"unknown shop {shop!r}")
    check_text(category, "category")
    if locale not in LOCALES:
        raise ShopCatalogError(f"locale must be 'ko', 'ja' or None, got {locale!r}")
    check_shop_url(source_url, shop, "source_url")
    with transaction(database_url, "catalog run insert") as connection:
        return connection.execute(
            "INSERT INTO shop_catalog_runs (shop, category, locale, source_url, status, started_at, updated_at)"
            " VALUES (%s, %s, %s, %s, 'running', clock_timestamp(), clock_timestamp()) RETURNING id",
            (shop, category, locale, source_url),
        ).fetchone()[0]


def get_run(database_url, run_id):
    """Run metadata and status with stored page/product counts and the sorted stored page numbers."""
    check_positive_int(run_id, "run_id")
    with transaction(database_url, "catalog run query") as connection:
        row = connection.execute(
            f"SELECT {', '.join(RUN_FIELDS)},"
            " (SELECT count(*) FROM shop_catalog_pages WHERE run_id = r.id),"
            " (SELECT count(*) FROM shop_catalog_products WHERE run_id = r.id),"
            " ARRAY(SELECT page_number FROM shop_catalog_pages WHERE run_id = r.id ORDER BY page_number),"
            " (SELECT count(*) FROM shop_catalog_failures WHERE run_id = r.id)"
            " FROM shop_catalog_runs r WHERE id = %s",
            (run_id,),
        ).fetchone()
    if row is None:
        raise ShopCatalogError(f"catalog run {run_id} does not exist")
    return {**dict(zip(RUN_FIELDS, row)), "page_count": row[-4], "product_count": row[-3], "stored_pages": row[-2],
            "failure_count": row[-1]}


def store_page(database_url, run_id, page_number, source_url, observed_at, body, parsed):
    """Store one page with its products atomically; returns the number of products inserted (0 on replay)."""
    check_positive_int(run_id, "run_id")
    check_positive_int(page_number, "page_number")
    check_observed_at(observed_at)
    if not isinstance(body, bytes) or not body:
        raise ShopCatalogError("body must be nonempty bytes")
    sha256 = hashlib.sha256(body).hexdigest()
    with transaction(database_url, "catalog page store") as connection:
        shop, status, total_products, total_pages = lock_run(connection, run_id)
        if status == "completed":
            raise ShopCatalogError(f"catalog run {run_id} is completed and cannot change")
        check_shop_url(source_url, shop, "source_url")
        check_parsed(parsed, page_number, shop)

        existing = connection.execute(
            "SELECT source_url = %s AND sha256 = %s AND observed_at = %s AND parsed = %s"
            " FROM shop_catalog_pages WHERE run_id = %s AND page_number = %s",
            (source_url, sha256, observed_at, Jsonb(parsed), run_id, page_number),
        ).fetchone()
        if existing is not None:
            if not existing[0]:
                raise ShopCatalogError(f"page {page_number} of run {run_id} is already stored with other content")
            return 0

        if total_products is None:
            connection.execute(
                "UPDATE shop_catalog_runs SET total_products = %s, total_pages = %s WHERE id = %s",
                (parsed["total_products"], parsed["total_pages"], run_id),
            )
        elif (total_products, total_pages) != (parsed["total_products"], parsed["total_pages"]):
            raise ShopCatalogError(
                f"page {page_number} totals {parsed['total_products']} products / {parsed['total_pages']} pages"
                f" differ from run totals {total_products} / {total_pages}"
            )

        products = parsed["products"]
        duplicates = connection.execute(
            "SELECT product_id FROM shop_catalog_products WHERE run_id = %s AND product_id = ANY(%s)",
            (run_id, [product["product_id"] for product in products]),
        ).fetchall()
        if duplicates:
            raise ShopCatalogError(
                f"page {page_number} repeats products already stored in run {run_id}: {[row[0] for row in duplicates]}"
            )

        connection.execute(
            "INSERT INTO shop_catalog_pages (run_id, page_number, source_url, observed_at, sha256, bytes, body_gzip,"
            " parsed, stored_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp())",
            (run_id, page_number, source_url, observed_at, sha256, len(body), gzip.compress(body, mtime=0),
             Jsonb(parsed)),
        )
        with connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO shop_catalog_products (run_id, page_number, product_id, name, product_url, locale,"
                " card_number, rarity_label, price_krw, list_price_krw, stock_status, product)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [(run_id, page_number, product["product_id"], product["name"], product["product_url"],
                  product["locale"], product.get("card_number"), product.get("rarity_label"),
                  product["price_krw"], product["list_price_krw"], product["stock_status"], Jsonb(product))
                 for product in products],
            )
        stored_products = connection.execute(
            "SELECT count(*) FROM shop_catalog_products WHERE run_id = %s", (run_id,)
        ).fetchone()[0]
        if stored_products > parsed["total_products"]:
            raise ShopCatalogError(
                f"run {run_id} would hold {stored_products} products, more than total {parsed['total_products']}"
            )
        # A valid new page resumes a failed run.
        connection.execute(
            "UPDATE shop_catalog_runs SET status = 'running', failed_at = NULL, error = NULL,"
            " updated_at = clock_timestamp() WHERE id = %s",
            (run_id,),
        )
    return len(products)


def fail_run(database_url, run_id, error):
    """Record a running or failed run as failed with this error; its committed pages are kept."""
    check_positive_int(run_id, "run_id")
    check_text(error, "error")
    with transaction(database_url, "catalog run failure update") as connection:
        status = lock_run(connection, run_id)[1]
        if status == "completed":
            raise ShopCatalogError(f"catalog run {run_id} is completed and cannot change")
        failed_at = connection.execute(
            "INSERT INTO shop_catalog_failures (run_id, failed_at, error) VALUES (%s, clock_timestamp(), %s)"
            " RETURNING failed_at",
            (run_id, error),
        ).fetchone()[0]
        connection.execute(
            "UPDATE shop_catalog_runs SET status = 'failed', failed_at = %s, error = %s, updated_at = %s"
            " WHERE id = %s",
            (failed_at, error, failed_at, run_id),
        )


def page_product_count(database_url, run_id, page_number):
    """Number of products stored for one stored page of a run; raises if that page is not stored."""
    check_positive_int(run_id, "run_id")
    check_positive_int(page_number, "page_number")
    with transaction(database_url, "catalog page query") as connection:
        row = connection.execute(
            "SELECT (SELECT count(*) FROM shop_catalog_products c"
            "        WHERE c.run_id = p.run_id AND c.page_number = p.page_number)"
            " FROM shop_catalog_pages p WHERE p.run_id = %s AND p.page_number = %s",
            (run_id, page_number),
        ).fetchone()
    if row is None:
        raise ShopCatalogError(f"page {page_number} of catalog run {run_id} is not stored")
    return row[0]


def reopen_run(database_url, run_id):
    """Explicitly return a failed run to 'running' so it can be resumed; its failure history is kept."""
    check_positive_int(run_id, "run_id")
    with transaction(database_url, "catalog run reopen") as connection:
        status = lock_run(connection, run_id)[1]
        if status != "failed":
            raise ShopCatalogError(f"catalog run {run_id} is {status}, only a failed run can be reopened")
        connection.execute(
            "UPDATE shop_catalog_runs SET status = 'running', failed_at = NULL, error = NULL,"
            " updated_at = clock_timestamp() WHERE id = %s",
            (run_id,),
        )


def complete_run(database_url, run_id):
    """Mark a running run completed only if pages 1..total_pages and exactly total_products products are stored."""
    check_positive_int(run_id, "run_id")
    with transaction(database_url, "catalog run completion") as connection:
        status = lock_run(connection, run_id)[1]
        if status != "running":
            raise ShopCatalogError(f"catalog run {run_id} is {status}, only a running run can complete")
        complete = connection.execute(
            "SELECT r.total_pages IS NOT NULL"
            " AND (SELECT count(*) FROM shop_catalog_pages p WHERE p.run_id = r.id) = r.total_pages"
            " AND (SELECT min(page_number) = 1 AND max(page_number) = r.total_pages"
            "      FROM shop_catalog_pages p WHERE p.run_id = r.id)"
            " AND (SELECT count(DISTINCT product_id) FROM shop_catalog_products c WHERE c.run_id = r.id)"
            "     = r.total_products"
            " FROM shop_catalog_runs r WHERE r.id = %s",
            (run_id,),
        ).fetchone()[0]
        if not complete:
            raise ShopCatalogError(f"catalog run {run_id} is missing pages or products and cannot complete")
        connection.execute(
            "UPDATE shop_catalog_runs SET status = 'completed', completed_at = clock_timestamp(),"
            " updated_at = clock_timestamp() WHERE id = %s",
            (run_id,),
        )
