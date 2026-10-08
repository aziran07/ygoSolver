"""PostgreSQL price history with a Redis 12-hour read cache.

PostgreSQL keeps every imported TCGSHOP listing snapshot and its per-product
observations. Redis only caches the answer to "current observed prices for one
exact card number + locale"; it never holds data that PostgreSQL does not.

This module never contacts the shop. Snapshots are captured elsewhere (a file
capture, or price_collector.py) and validated by `prices.parse_tcgshop_list`.

Shop request gate: TCGSHOP robots.txt declares a global `Crawl-delay: 43200`.
price_collection_gate holds the one global time the next shop request is
allowed, shared by every app session and process. A request is reserved
(gate advanced by 12 hours, attempt row inserted) in one transaction before
any HTTP is sent, so a failed or interrupted request also uses the interval.
`init` seeds the gate from the newest stored snapshot's observed_at, and a
reservation also waits for 12 hours after any snapshot imported later.

Cache consistency:
- Every cache key carries a group revision: the highest committed snapshot id
  that contains the card number + locale. A cache hit therefore still runs one
  indexed PostgreSQL query to learn the current revision.
- Imports hold a table lock until commit, so snapshot ids become visible in
  id order. The revision and the latest-per-product rows are read in one
  REPEATABLE READ transaction, bounded by that revision.
- Each product's latest observation is chosen by observed_at, so importing an
  older capture later never replaces a newer observed price.
- A cached entry expires at the earliest observed_at + 12 hours (absolute,
  SET PXAT); it is never extended by another 12 hours.

Usage (DATABASE_URL and REDIS_URL from the environment):
    python price_store.py init
    python price_store.py import --html PATH --metadata PATH
    python price_store.py get --card-number YAC1-JP002 --locale ja
"""

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import psycopg
from psycopg.conninfo import conninfo_to_dict
import redis

from prices import CATEGORY_LOCALES, REGION_LOCALES, SCOPE, PriceError, load_snapshot, parse_tcgshop_list

PRICE_LIFETIME = timedelta(hours=12)
CRAWL_DELAY = timedelta(seconds=43200)  # TCGSHOP robots.txt, global for the whole site
GATE_NAME = "tcgshop"
RESULT_SCOPE = "observed_products"
CACHE_KEY_PREFIX = "ygosolver:prices:v1"
CACHE_FORMAT = 1
CARD_NUMBER = re.compile(r"[0-9A-Z]{2,6}-(JP|KR)[A-Z]{0,2}[0-9]{2,3}")
LOCALES = set(CATEGORY_LOCALES.values())
STOCK_STATUSES = ("in_stock", "out_of_stock", "unknown")
PRODUCT_URL_PREFIX = "http://www.tcgshop.co.kr/goods_detail.php?goodsIdx="
# Order of the fields returned for every observed product.
PRODUCT_FIELDS = (
    "product_id", "name", "card_number", "locale", "rarity_label", "price_krw",
    "stock_status", "stock_evidence", "product_url", "observed_at",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS price_snapshots (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_url TEXT NOT NULL,
    locale TEXT NOT NULL CHECK (locale IN ('ja', 'ko')),
    scope TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    status_code INTEGER NOT NULL CHECK (status_code = 200),
    sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    bytes INTEGER NOT NULL CHECK (bytes > 0),
    content_type TEXT,
    imported_at TIMESTAMPTZ NOT NULL,
    UNIQUE (source_url, observed_at)
);
CREATE TABLE IF NOT EXISTS price_observations (
    snapshot_id BIGINT NOT NULL REFERENCES price_snapshots(id),
    product_id TEXT NOT NULL,
    name TEXT NOT NULL,
    card_number TEXT NOT NULL,
    locale TEXT NOT NULL CHECK (locale IN ('ja', 'ko')),
    rarity_label TEXT NOT NULL,
    price_krw INTEGER NOT NULL CHECK (price_krw > 0),
    stock_status TEXT NOT NULL CHECK (stock_status IN ('in_stock', 'out_of_stock', 'unknown')),
    stock_evidence TEXT NOT NULL,
    product_url TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, product_id)
);
CREATE INDEX IF NOT EXISTS price_observations_group
    ON price_observations (card_number, locale, snapshot_id);
CREATE TABLE IF NOT EXISTS price_collection_gate (
    name TEXT PRIMARY KEY CHECK (name = 'tcgshop'),
    last_request_at TIMESTAMPTZ,
    next_allowed_at TIMESTAMPTZ NOT NULL,
    seeded_from TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS price_collection_attempts (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    card_number TEXT NOT NULL,
    locale TEXT NOT NULL CHECK (locale IN ('ja', 'ko')),
    request_url TEXT NOT NULL,
    reserved_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    outcome TEXT NOT NULL CHECK (outcome IN ('pending', 'stored', 'failed')),
    snapshot_id BIGINT REFERENCES price_snapshots(id),
    product_count INTEGER,
    error TEXT,
    CHECK ((outcome = 'pending') = (finished_at IS NULL)),
    CHECK ((outcome = 'stored') = (snapshot_id IS NOT NULL AND product_count > 0)),
    CHECK ((outcome = 'failed') = (error IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS price_collection_attempts_number
    ON price_collection_attempts (card_number, locale, id);
"""

# The last shop request known before the gate existed is the newest stored snapshot; without any, the first request
# is allowed now. Existing gate rows are never changed.
SEED_GATE = """
INSERT INTO price_collection_gate (name, last_request_at, next_allowed_at, seeded_from)
SELECT %(name)s, max(observed_at), coalesce(max(observed_at) + %(delay)s, now()),
       CASE WHEN max(observed_at) IS NULL THEN 'no stored snapshot' ELSE 'max(price_snapshots.observed_at)' END
FROM price_snapshots
ON CONFLICT (name) DO NOTHING
"""
ATTEMPT_FIELDS = ("id", "card_number", "locale", "request_url", "reserved_at", "finished_at", "outcome",
                  "snapshot_id", "product_count", "error")


class PriceNotFound(PriceError):
    pass


class StalePriceError(PriceError):
    pass


class PriceDatabaseError(PriceError):
    """PostgreSQL could not be reached or a statement failed."""


class PriceCacheError(PriceError):
    """Redis could not be reached or a command failed."""


def redact(text, *urls):
    """Remove passwords contained in the given connection URLs from text."""
    for url in urls:
        password = urlsplit(url).password if url else None
        if password:
            text = text.replace(password, "***")
    return text


def connect_postgres(database_url):
    try:
        parameters = conninfo_to_dict(database_url)
        parameters.setdefault("connect_timeout", 5)
        return psycopg.connect(**parameters)
    except psycopg.Error as error:
        raise PriceDatabaseError(f"PostgreSQL connection failed: {redact(str(error), database_url)}") from error


def init_db(database_url):
    """Create the tables and seed the shop request gate in one transaction (safe to repeat)."""
    connection = connect_postgres(database_url)
    try:
        with connection:
            connection.execute(SCHEMA)
            connection.execute(SEED_GATE, {"name": GATE_NAME, "delay": CRAWL_DELAY})
    except psycopg.Error as error:
        raise PriceError(f"PostgreSQL schema creation failed: {redact(str(error), database_url)}") from error
    finally:
        connection.close()


def import_snapshot(html_path, metadata_path, database_url, redis_url):
    """Store one captured listing snapshot file atomically; re-importing it is a no-op."""
    try:
        metadata, html = load_snapshot(html_path, metadata_path)
    except (OSError, ValueError) as error:
        raise PriceError(f"cannot read snapshot files: {error}") from error
    return store_listing(metadata, parse_tcgshop_list(html, metadata["url"]), database_url)


def store_listing(metadata, products, database_url, attempt_id=None, scope=SCOPE):
    """Store one validated listing response (metadata + parsed products) atomically.

    With attempt_id, the pending collection attempt is marked stored in the same transaction. Redis is not
    touched: cache keys are versioned by snapshot id, so a new snapshot makes new keys instead of invalidating
    old ones.
    """
    if not products:
        raise PriceError("a snapshot must contain at least one product")
    source_url = metadata["url"]
    observed_at = datetime.fromisoformat(metadata["observed_at"])
    summary = {
        "source_url": source_url,
        "locale": products[0]["locale"],
        "observed_at": observed_at.astimezone(timezone.utc).isoformat(),
        "sha256": metadata["sha256"],
        "product_count": len(products),
        "stock_counts": dict(Counter(product["stock_status"] for product in products)),
    }

    connection = connect_postgres(database_url)
    try:
        with connection:
            # Self-conflicting lock held until commit: imports run one at a
            # time, so snapshot ids are committed in increasing order.
            connection.execute("LOCK TABLE price_snapshots IN SHARE ROW EXCLUSIVE MODE")
            existing = connection.execute(
                "SELECT id, sha256 FROM price_snapshots WHERE source_url = %s AND observed_at = %s",
                (source_url, observed_at),
            ).fetchone()
            if existing:
                if attempt_id is not None:
                    raise PriceError(f"collected snapshot {source_url} at {summary['observed_at']} already exists")
                if existing[1] != metadata["sha256"]:
                    raise PriceError(
                        f"snapshot {source_url} at {summary['observed_at']} already stored with another sha256"
                    )
                return {**summary, "snapshot_id": existing[0], "inserted": 0, "already_imported": True}
            snapshot_id = connection.execute(
                "INSERT INTO price_snapshots (source_url, locale, scope, observed_at, status_code, sha256, bytes,"
                " content_type, imported_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now()) RETURNING id",
                (source_url, summary["locale"], scope, observed_at, metadata["status_code"],
                 metadata["sha256"], metadata["bytes"], metadata.get("content_type")),
            ).fetchone()[0]
            with connection.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO price_observations (snapshot_id, product_id, name, card_number, locale,"
                    " rarity_label, price_krw, stock_status, stock_evidence, product_url) VALUES"
                    " (%(snapshot_id)s, %(product_id)s, %(name)s, %(card_number)s, %(locale)s, %(rarity_label)s,"
                    " %(price_krw)s, %(stock_status)s, %(stock_evidence)s, %(product_url)s)",
                    [{**product, "snapshot_id": snapshot_id} for product in products],
                )
            if attempt_id is not None:
                updated = connection.execute(
                    "UPDATE price_collection_attempts SET outcome = 'stored', finished_at = clock_timestamp(),"
                    " snapshot_id = %s, product_count = %s WHERE id = %s AND outcome = 'pending'",
                    (snapshot_id, len(products), attempt_id),
                ).rowcount
                if updated != 1:
                    raise PriceError(f"collection attempt {attempt_id} is not pending")
    except psycopg.Error as error:
        raise PriceDatabaseError(f"PostgreSQL import failed: {redact(str(error), database_url)}") from error
    finally:
        connection.close()
    return {**summary, "snapshot_id": snapshot_id, "inserted": len(products), "already_imported": False}


def reserve_collection(card_number, locale, request_url, database_url):
    """Reserve the one global shop request if the gate allows it now (PostgreSQL clock).

    Returns {"allowed": True, "attempt_id", "reserved_at", "next_allowed_at"} after advancing the gate by
    CRAWL_DELAY and inserting a pending attempt, or {"allowed": False, "next_allowed_at"}. The row lock makes
    concurrent sessions and processes take turns, so at most one of them is allowed per interval.
    """
    connection = connect_postgres(database_url)
    try:
        with connection:
            gate = connection.execute(
                "SELECT next_allowed_at FROM price_collection_gate WHERE name = %s FOR UPDATE", (GATE_NAME,),
            ).fetchone()
            if gate is None:
                raise PriceError("shop request gate is not initialized; run `price_store.py init`")
            next_allowed_at = gate[0]
            # Read after the row lock is held, so time spent waiting for another session counts and the
            # interval starts at the actual reservation.
            now = connection.execute("SELECT clock_timestamp()").fetchone()[0]
            # A capture imported from a file after the gate was seeded is also a shop request.
            newest_snapshot = connection.execute("SELECT max(observed_at) FROM price_snapshots").fetchone()[0]
            if newest_snapshot is not None:
                next_allowed_at = max(next_allowed_at, newest_snapshot + CRAWL_DELAY)
            if now < next_allowed_at:
                return {"allowed": False, "next_allowed_at": next_allowed_at}
            connection.execute(
                "UPDATE price_collection_gate SET last_request_at = %s, next_allowed_at = %s WHERE name = %s",
                (now, now + CRAWL_DELAY, GATE_NAME),
            )
            attempt_id = connection.execute(
                "INSERT INTO price_collection_attempts (card_number, locale, request_url, reserved_at, outcome)"
                " VALUES (%s, %s, %s, %s, 'pending') RETURNING id",
                (card_number, locale, request_url, now),
            ).fetchone()[0]
    except psycopg.Error as error:
        raise PriceDatabaseError(f"PostgreSQL request gate failed: {redact(str(error), database_url)}") from error
    finally:
        connection.close()
    return {"allowed": True, "attempt_id": attempt_id, "reserved_at": now, "next_allowed_at": now + CRAWL_DELAY}


def finish_attempt(attempt_id, error, database_url):
    """Record a pending collection attempt as failed with its error text."""
    connection = connect_postgres(database_url)
    try:
        with connection:
            updated = connection.execute(
                "UPDATE price_collection_attempts SET outcome = 'failed', finished_at = clock_timestamp(), error = %s"
                " WHERE id = %s AND outcome = 'pending'",
                (error, attempt_id),
            ).rowcount
            if updated != 1:
                raise PriceError(f"collection attempt {attempt_id} is not pending")
    except psycopg.Error as error_:
        raise PriceDatabaseError(f"PostgreSQL attempt update failed: {redact(str(error_), database_url)}") from error_
    finally:
        connection.close()


def latest_attempt(card_number, locale, database_url):
    """The newest collection attempt for an exact card number + locale as a dict, or None."""
    connection = connect_postgres(database_url)
    try:
        row = connection.execute(
            f"SELECT {', '.join(ATTEMPT_FIELDS)} FROM price_collection_attempts"
            " WHERE card_number = %s AND locale = %s ORDER BY id DESC LIMIT 1",
            (card_number, locale),
        ).fetchone()
    except psycopg.Error as error:
        raise PriceDatabaseError(f"PostgreSQL attempt query failed: {redact(str(error), database_url)}") from error
    finally:
        connection.close()
    return dict(zip(ATTEMPT_FIELDS, row)) if row else None


def cache_key(card_number, locale, revision):
    return f"{CACHE_KEY_PREFIX}:{locale}:{card_number}:r{revision}"


def parse_utc(value, label):
    """Parse an aware ISO timestamp; anything else is corrupt data."""
    if not isinstance(value, str):
        raise PriceError(f"{label} is not a timestamp string: {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise PriceError(f"{label} is not an ISO timestamp: {value!r}") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise PriceError(f"{label} is not a UTC timestamp: {value!r}")
    return parsed


def check_freshness(card_number, locale, observed_times, now):
    """Return the absolute expiry of the group, or fail if any price is unusable."""
    for observed_at in observed_times:
        if observed_at > now:
            raise PriceError(f"{card_number} {locale}: observed_at {observed_at.isoformat()} is in the future")
        if observed_at + PRICE_LIFETIME <= now:
            raise StalePriceError(
                f"{card_number} {locale}: price observed at {observed_at.isoformat()} expired after 12 hours"
            )
    return min(observed_times) + PRICE_LIFETIME


def validate_cached(entry, card_number, locale, revision, now):
    """Check a decoded cache envelope completely; raise PriceError when corrupt."""
    expected_keys = {"format", "card_number", "locale", "revision", "scope", "expires_at", "prices"}
    if not isinstance(entry, dict) or set(entry) != expected_keys:
        raise PriceError("cache envelope has unexpected shape")
    identity = (entry["format"], entry["card_number"], entry["locale"], entry["revision"], entry["scope"])
    if identity != (CACHE_FORMAT, card_number, locale, revision, RESULT_SCOPE) or type(entry["revision"]) is not int:
        raise PriceError(f"cache envelope identity {identity!r} does not match the request")
    prices = entry["prices"]
    if not isinstance(prices, list) or not prices:
        raise PriceError("cache envelope has no prices")
    observed_times = []
    product_ids = set()
    for product in prices:
        if not isinstance(product, dict) or set(product) != set(PRODUCT_FIELDS):
            raise PriceError("cached product has unexpected fields")
        product_id = product["product_id"]
        if not isinstance(product_id, str) or not product_id.isdigit() or product_id in product_ids:
            raise PriceError(f"cached product id {product_id!r} is invalid or duplicated")
        product_ids.add(product_id)
        if product["card_number"] != card_number or product["locale"] != locale:
            raise PriceError(f"cached product {product_id} belongs to another card or locale")
        for field in ("name", "rarity_label", "stock_evidence"):
            if not isinstance(product[field], str) or not product[field]:
                raise PriceError(f"cached product {product_id} has invalid {field}")
        price = product["price_krw"]
        if type(price) is not int or price <= 0:
            raise PriceError(f"cached product {product_id} has invalid price_krw {price!r}")
        if product["stock_status"] not in STOCK_STATUSES:
            raise PriceError(f"cached product {product_id} has invalid stock_status {product['stock_status']!r}")
        if product["product_url"] != PRODUCT_URL_PREFIX + product_id:
            raise PriceError(f"cached product {product_id} has invalid product_url")
        observed_times.append(parse_utc(product["observed_at"], f"cached product {product_id} observed_at"))
    expires_at = check_freshness(card_number, locale, observed_times, now)
    if parse_utc(entry["expires_at"], "cache expires_at") != expires_at:
        raise PriceError("cache expires_at does not match the earliest observed_at + 12 hours")


def validate_entry(entry, card_number, locale, revision, now, origin):
    """validate_cached with the data origin named in any corruption error."""
    try:
        validate_cached(entry, card_number, locale, revision, now)
    except StalePriceError:
        raise
    except PriceError as error:
        raise PriceError(f"{origin} is corrupt: {error}") from error


def query_revision(connection, card_number, locale):
    """Highest committed snapshot id containing this group (index-only lookup)."""
    return connection.execute(
        "SELECT max(snapshot_id) FROM price_observations WHERE card_number = %s AND locale = %s",
        (card_number, locale),
    ).fetchone()[0]


def query_latest_products(connection, card_number, locale, revision):
    """Latest observation per product (by observed_at) among snapshots up to revision."""
    return connection.execute(
        "SELECT DISTINCT ON (o.product_id) o.product_id, o.name, o.card_number, o.locale, o.rarity_label,"
        " o.price_krw, o.stock_status, o.stock_evidence, o.product_url, s.observed_at"
        " FROM price_observations o JOIN price_snapshots s ON s.id = o.snapshot_id"
        " WHERE o.card_number = %s AND o.locale = %s AND o.snapshot_id <= %s"
        " ORDER BY o.product_id, s.observed_at DESC, o.snapshot_id DESC",
        (card_number, locale, revision),
    ).fetchall()


def build_entry(card_number, locale, revision, rows):
    observed_times = [row[-1].astimezone(timezone.utc) for row in rows]
    prices = [
        dict(zip(PRODUCT_FIELDS, (*row[:-1], observed_at.isoformat())))
        for row, observed_at in zip(rows, observed_times)
    ]
    return {
        "format": CACHE_FORMAT,
        "card_number": card_number,
        "locale": locale,
        "revision": revision,
        "scope": RESULT_SCOPE,
        "expires_at": (min(observed_times) + PRICE_LIFETIME).isoformat(),
        "prices": prices,
    }


def get_prices(card_number, locale, database_url, redis_url):
    """Current observed TCGSHOP products for an exact card number + locale.

    Covers only products seen in imported listing snapshots; this is not the
    retailer's full catalog. Raises PriceNotFound, StalePriceError or PriceError.
    A cache hit still runs the revision query against PostgreSQL.
    """
    number_match = CARD_NUMBER.fullmatch(card_number) if isinstance(card_number, str) else None
    if not number_match:
        raise PriceError(f"invalid card number {card_number!r}")
    if locale not in LOCALES:
        raise PriceError(f"invalid locale {locale!r}")
    if REGION_LOCALES[number_match.group(1)] != locale:
        raise PriceError(f"card number {card_number} is not a {locale} card number")

    connection = connect_postgres(database_url)
    try:
        # Revision, cache lookup and (on a miss) the product rows share one
        # consistent snapshot of the database.
        connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        connection.read_only = True
        with connection.transaction():
            revision = query_revision(connection, card_number, locale)
            if revision is None:
                raise PriceNotFound(f"no observed TCGSHOP price for {card_number} {locale}")
            key = cache_key(card_number, locale, revision)
            with redis.Redis.from_url(redis_url, socket_timeout=5, socket_connect_timeout=5) as cache:
                cached = cache.get(key)
                if cached is not None:
                    try:
                        entry = json.loads(cached)
                    except (UnicodeDecodeError, json.JSONDecodeError) as error:
                        raise PriceError(f"cache entry {key} is not valid JSON") from error
                    validate_entry(
                        entry, card_number, locale, revision, datetime.now(timezone.utc), f"cache entry {key}"
                    )
                    cache_status = "hit"
                else:
                    rows = query_latest_products(connection, card_number, locale, revision)
                    entry = build_entry(card_number, locale, revision, rows)
                    # Read the clock after the query so a slow fetch cannot
                    # validate rows against a time before it finished.
                    validated_at = datetime.now(timezone.utc)
                    validate_entry(
                        entry, card_number, locale, revision, validated_at, f"database rows for revision {revision}"
                    )
                    expires_ms = int(datetime.fromisoformat(entry["expires_at"]).timestamp() * 1000)
                    cache.set(key, json.dumps(entry, ensure_ascii=False), pxat=expires_ms)
                    cache_status = "miss"
    except psycopg.Error as error:
        raise PriceDatabaseError(f"PostgreSQL price query failed: {redact(str(error), database_url)}") from error
    except redis.RedisError as error:
        raise PriceCacheError(f"Redis cache failed: {redact(str(error), redis_url)}") from error
    finally:
        connection.close()
    return {
        "card_number": card_number,
        "locale": locale,
        "scope": RESULT_SCOPE,
        "prices": entry["prices"],
        "expires_at": entry["expires_at"],
        "cache_status": cache_status,
    }


def required_env(name):
    value = os.environ.get(name)
    if not value:
        raise PriceError(f"environment variable {name} is not set")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create the PostgreSQL tables")
    import_command = commands.add_parser("import", help="import a captured listing snapshot")
    import_command.add_argument("--html", required=True)
    import_command.add_argument("--metadata", required=True)
    get_command = commands.add_parser("get", help="current observed prices for one card number")
    get_command.add_argument("--card-number", required=True)
    get_command.add_argument("--locale", required=True, choices=sorted(LOCALES))
    arguments = parser.parse_args()

    stage = "config"
    try:
        database_url = required_env("DATABASE_URL")
        if arguments.command == "init":
            stage = "init"
            init_db(database_url)
            result = {"initialized": True}
        elif arguments.command == "import":
            stage = "import"
            result = import_snapshot(arguments.html, arguments.metadata, database_url, None)
        else:
            redis_url = required_env("REDIS_URL")
            stage = "get"
            result = get_prices(arguments.card_number, arguments.locale, database_url, redis_url)
    except PriceError as error:
        print(f"{stage} failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
