"""Import a captured TCGSHOP card listing page into a local SQLite price history.

This module never contacts the shop. It parses an HTML response that was already
captured (raw bytes + metadata JSON) and stores one observation per product.

TCGSHOP robots.txt declares a global `Crawl-delay: 43200` (12 hours between any
two requests to the site). One listing page is one snapshot, never the whole
catalog. The app's on-demand single-card collection (price_collector.py) does
not apply that delay.

Usage:
    python prices.py import --html PATH --metadata PATH --db PATH
"""

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

LIST_URL_PREFIX = "http://www.tcgshop.co.kr/goods_list.php"
# Shop category Index -> card locale. The card number region must agree.
CATEGORY_LOCALES = {"288": "ja", "276": "ko"}
REGION_LOCALES = {"JP": "ja", "KR": "ko"}
CARD_NUMBER = re.compile(r"\(([0-9A-Z]{2,6}-(JP|KR)[A-Z]{0,2}[0-9]{2,3})\)")
PRICE = re.compile(r"[0-9]{1,3}(,[0-9]{3})*")
SCOPE = "single_list_page"

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    source_url TEXT NOT NULL,
    locale TEXT NOT NULL CHECK (locale IN ('ja', 'ko')),
    scope TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    content_type TEXT,
    imported_at TEXT NOT NULL,
    UNIQUE (source_url, observed_at)
);
CREATE TABLE IF NOT EXISTS price_observations (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
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
"""


class PriceError(Exception):
    pass


def locale_for_list_url(source_url):
    if not source_url.startswith(LIST_URL_PREFIX + "?"):
        raise PriceError(f"not a TCGSHOP goods_list URL: {source_url}")
    query = parse_qs(urlsplit(source_url).query)
    indexes = query.get("Index", [])
    if len(indexes) != 1 or indexes[0] not in CATEGORY_LOCALES:
        raise PriceError(f"unknown TCGSHOP category Index in {source_url}")
    return CATEGORY_LOCALES[indexes[0]]


def single_text(block, selector, product_id):
    elements = block.select(selector)
    if len(elements) != 1:
        raise PriceError(f"product {product_id}: expected one {selector}, found {len(elements)}")
    text = " ".join(elements[0].get_text(" ").split())
    if not text:
        raise PriceError(f"product {product_id}: empty {selector}")
    return text


def hidden_value(block, name, product_id):
    inputs = block.select(f'input[name="{name}"]')
    if len(inputs) > 1:
        raise PriceError(f"product {product_id}: duplicate {name}")
    if not inputs:
        return None
    return inputs[0].get("value", "")


def parse_stock(block, product_id):
    """Listing cart button evidence only; checkout was never verified."""
    cart_images = [
        image for image in block.find_all("img")
        if image.get("src", "").rsplit("/", 1)[-1] in ("go_cart.gif", "dis_go_cart.gif")
    ]
    if len(cart_images) > 1:
        raise PriceError(f"product {product_id}: {len(cart_images)} cart buttons")
    limit_flag = hidden_value(block, "comparechk_bLimit", product_id)
    limit_count_text = hidden_value(block, "comparechk_limitCnt", product_id)
    limit_count = None
    if limit_count_text is not None:
        if not re.fullmatch(r"-?[0-9]+", limit_count_text):
            raise PriceError(f"product {product_id}: invalid comparechk_limitCnt {limit_count_text!r}")
        limit_count = int(limit_count_text)
    limit_evidence = f"comparechk_bLimit={limit_flag}; comparechk_limitCnt={limit_count_text}"

    cart_file = None
    handler_ids = []
    evidence = f"no cart button; {limit_evidence}"
    if cart_images:
        cart_file = cart_images[0]["src"].rsplit("/", 1)[-1]
        onclick = cart_images[0].get("onclick", "")
        handler_ids = re.findall(r"cartOneGo\('([^']*)'\)", onclick)
        if any(handler_id != product_id for handler_id in handler_ids):
            raise PriceError(f"product {product_id}: cart handler targets {handler_ids}")
        evidence = f"{cart_file} onclick={onclick!r}; {limit_evidence}"

    if "품절" in block.get_text():
        if cart_file == "go_cart.gif":
            raise PriceError(f"product {product_id}: shows 품절 and an active cart button")
        return "out_of_stock", f"listing shows 품절; {evidence}"
    if cart_file != "go_cart.gif" or handler_ids != [product_id]:
        return "unknown", f"no active cart button for {product_id}; {evidence}"
    if limit_flag != "1" or limit_count is None:
        return "unknown", f"active cart button without purchase limit; {evidence}"
    if limit_count <= 0:
        raise PriceError(f"product {product_id}: active cart button but limit {limit_count}")
    # limitCnt is kept as evidence only; it is not verified inventory.
    return "in_stock", f"listing cart button active; {evidence}"


def parse_tcgshop_list(html, source_url):
    locale = locale_for_list_url(source_url)
    soup = BeautifulSoup(html, "html.parser")
    page_indexes = [field.get("value") for field in soup.select('input[name="Index"]')]
    if len(page_indexes) != 1 or CATEGORY_LOCALES.get(page_indexes[0]) != locale:
        raise PriceError(f"page category {page_indexes} does not match {source_url}")
    blocks = soup.select("table[id^=list_card_]")
    if not blocks:
        raise PriceError("no list_card_ product blocks")

    products = []
    seen_ids = set()
    for block in blocks:
        match = re.fullmatch(r"list_card_([0-9]+)", block["id"])
        if not match:
            raise PriceError(f"unrecognized product block id {block['id']!r}")
        product_id = match.group(1)
        if product_id in seen_ids:
            raise PriceError(f"duplicate product id {product_id}")
        seen_ids.add(product_id)
        if block.select("table[id^=list_card_]"):
            raise PriceError(f"product {product_id}: nested product block")

        detail_href = f"goods_detail.php?goodsIdx={product_id}"
        for link in block.select("a[href]"):
            if "goodsIdx=" in link["href"] and link["href"] != detail_href:
                raise PriceError(f"product {product_id}: link to {link['href']}")
        name_links = block.select(".glist_01 a[href]")
        if [link["href"] for link in name_links] != [detail_href]:
            raise PriceError(f"product {product_id}: name is not linked to its detail page")
        name = single_text(block, ".glist_01", product_id)

        number_text = single_text(block, ".glist_02", product_id)
        number_match = CARD_NUMBER.fullmatch(number_text)
        if not number_match:
            raise PriceError(f"product {product_id}: unrecognized card number {number_text!r}")
        card_number, region = number_match.groups()
        if REGION_LOCALES[region] != locale:
            raise PriceError(f"product {product_id}: {card_number} is not a {locale} card number")

        rarity_parts = [" ".join(span.get_text(" ").split()) for span in block.select(".glist_03")]
        rarity_parts = [part for part in rarity_parts if part]
        if not rarity_parts:
            raise PriceError(f"product {product_id}: missing rarity")

        price_text = single_text(block, ".glist_price12", product_id)
        currency = single_text(block, ".glist_price_won", product_id)
        if currency != "원":
            raise PriceError(f"product {product_id}: price unit {currency!r} is not 원")
        if not PRICE.fullmatch(price_text) or int(price_text.replace(",", "")) <= 0:
            raise PriceError(f"product {product_id}: invalid price {price_text!r}")

        stock_status, stock_evidence = parse_stock(block, product_id)
        products.append({
            "product_id": product_id,
            "name": name,
            "card_number": card_number,
            "locale": locale,
            "rarity_label": " ".join(rarity_parts),
            "price_krw": int(price_text.replace(",", "")),
            "stock_status": stock_status,
            "stock_evidence": stock_evidence,
            "product_url": urljoin(source_url, detail_href),
        })
    return products


def load_snapshot(html_path, metadata_path):
    metadata = json.loads(Path(metadata_path).read_text(encoding="utf-8"))
    for key in ("url", "observed_at", "status_code", "sha256", "bytes"):
        if key not in metadata:
            raise PriceError(f"metadata missing {key}")
    if metadata["status_code"] != 200:
        raise PriceError(f"captured status {metadata['status_code']}, expected 200")
    body = Path(html_path).read_bytes()
    if len(body) != metadata["bytes"]:
        raise PriceError(f"snapshot is {len(body)} bytes, metadata says {metadata['bytes']}")
    digest = hashlib.sha256(body).hexdigest()
    if digest != metadata["sha256"]:
        raise PriceError(f"snapshot sha256 {digest} does not match metadata")
    try:
        observed = datetime.fromisoformat(metadata["observed_at"])
    except (TypeError, ValueError) as error:
        raise PriceError(f"invalid observed_at {metadata['observed_at']!r}") from error
    if observed.tzinfo is None:
        raise PriceError("observed_at has no timezone")
    if observed > datetime.now(timezone.utc):
        raise PriceError(f"observed_at {metadata['observed_at']} is in the future")
    try:
        html = body.decode("euc-kr")
    except UnicodeDecodeError as error:
        raise PriceError(f"snapshot is not valid EUC-KR: {error}") from error
    return metadata, html


def import_snapshot(html_path, metadata_path, database_path):
    metadata, html = load_snapshot(html_path, metadata_path)
    source_url = metadata["url"]
    products = parse_tcgshop_list(html, source_url)
    summary = {
        "source_url": source_url,
        "locale": products[0]["locale"],
        "observed_at": metadata["observed_at"],
        "sha256": metadata["sha256"],
        "product_count": len(products),
        "stock_counts": dict(Counter(product["stock_status"] for product in products)),
    }

    Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(SCHEMA)
        with connection:
            existing = connection.execute(
                "SELECT id, sha256 FROM snapshots WHERE source_url = ? AND observed_at = ?",
                (source_url, metadata["observed_at"]),
            ).fetchone()
            if existing:
                if existing[1] != metadata["sha256"]:
                    raise PriceError(f"snapshot {source_url} at {metadata['observed_at']} already stored with another sha256")
                return {**summary, "snapshot_id": existing[0], "inserted": 0, "already_imported": True}
            snapshot_id = connection.execute(
                "INSERT INTO snapshots (source_url, locale, scope, observed_at, status_code, sha256, bytes,"
                " content_type, imported_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (source_url, summary["locale"], SCOPE, metadata["observed_at"], metadata["status_code"],
                 metadata["sha256"], metadata["bytes"], metadata.get("content_type"),
                 datetime.now(timezone.utc).isoformat()),
            ).lastrowid
            connection.executemany(
                "INSERT INTO price_observations (snapshot_id, product_id, name, card_number, locale, rarity_label,"
                " price_krw, stock_status, stock_evidence, product_url) VALUES"
                " (:snapshot_id, :product_id, :name, :card_number, :locale, :rarity_label,"
                " :price_krw, :stock_status, :stock_evidence, :product_url)",
                [{**product, "snapshot_id": snapshot_id} for product in products],
            )
    finally:
        connection.close()
    return {**summary, "snapshot_id": snapshot_id, "inserted": len(products), "already_imported": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    import_command = commands.add_parser("import", help="import a captured listing snapshot")
    import_command.add_argument("--html", required=True)
    import_command.add_argument("--metadata", required=True)
    import_command.add_argument("--db", required=True)
    arguments = parser.parse_args()
    try:
        summary = import_snapshot(arguments.html, arguments.metadata, arguments.db)
    except PriceError as error:
        print(f"import failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
