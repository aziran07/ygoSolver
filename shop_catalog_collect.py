"""Collect every listing page of one configured Cardmoa or TCGSHOP category into the shop_catalog_* tables.

One invocation collects one (shop, category) run, page by page, in order:
- Exactly one GET per page: no redirects followed, no retry, timeout (5, 20) seconds. Only HTTP 200 with a
  text/html content type and a nonempty body is parsed (shop_catalog_parse); the parsed page and its exact
  response bytes are stored together (shop_catalog_store.store_page). Product rows go only to the separate
  shop_catalog_* tables, never to the price_* tables the app reads.
- Request starts are at least `delay` seconds apart (default 1.0, minimum 1), measured with a monotonic clock
  from the previous request's start, so fetch and parse time count toward the delay. There is no parallelism
  inside a run; different shops can be collected by separate processes.
- Once the run is open, any failure (reading its stored state, a page request, response, parse or store, or the
  completion) records the run as failed (shop_catalog_store.fail_run) with the stage, page number, URL and cause,
  and raises CatalogCollectionError. Invalid arguments, and a run of another shop/category or an already completed
  run, are rejected before the run is changed (CLI exit status 2 for invalid arguments). Committed pages are kept. If recording the
  failure also fails, the raised error names both causes. With evidence_dir, the failing response body is saved
  there with its request URL, status, observation time and SHA-256 (no request or response headers).
- run_id resumes an existing run of the same shop and category: stored pages are never requested again, and a
  failed run is reopened (its failure history is kept). Without run_id a new run is started.
- max_pages limits the number of new pages requested by this invocation. The run is completed only when every
  page 1..total_pages is stored; otherwise it stays 'running' and the CLI exits with status 3.

TCGSHOP robots.txt declares `Crawl-delay: 43200` (12 hours between requests) for the whole site. This collector
does not apply that delay; it uses `delay` above.

Usage (DATABASE_URL in the environment):
    python shop_catalog_collect.py init
    python shop_catalog_collect.py collect --shop cardmoa --category 101010 [--run-id ID] [--max-pages N]
                                           [--delay 1.0] [--evidence-dir DIR]
    python shop_catalog_collect.py status --run-id ID
"""

import argparse
import hashlib
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

import shop_catalog_parse as parser
import shop_catalog_store as store
from price_store import required_env
from prices import PriceError

TIMEOUT_SECONDS = (5, 20)  # connect, read
USER_AGENT = "ygoSolver shop catalog collection (one category, sequential pages)"
STORED_PARSED_KEYS = ("products", "total_products", "total_pages")
EXIT_INCOMPLETE = 3


class CatalogCollectionError(store.ShopCatalogError):
    """A page request, its response, parsing, storage or run completion failed; the run records the cause."""


def check_arguments(shop, category, run_id, max_pages, delay):
    if (shop, category) not in parser.CATEGORIES:
        raise CatalogCollectionError(f"unconfigured catalog category {shop!r} {category!r}")
    if run_id is not None and (type(run_id) is not int or run_id < 1):
        raise CatalogCollectionError(f"run_id must be a positive integer or None, got {run_id!r}")
    if max_pages is not None and (type(max_pages) is not int or max_pages < 1):
        raise CatalogCollectionError(f"max_pages must be a positive integer or None, got {max_pages!r}")
    if type(delay) not in (int, float) or not math.isfinite(delay) or delay < 1:
        raise CatalogCollectionError(f"delay must be a finite number of seconds >= 1, got {delay!r}")


def open_run(database_url, shop, category, run_id):
    """The run to collect into: a new one, or the given one after checking it may be resumed."""
    if run_id is None:
        return store.start_run(database_url, shop, category, parser.CATEGORIES[(shop, category)],
                               parser.page_url(shop, category))
    run = store.get_run(database_url, run_id)
    if (run["shop"], run["category"]) != (shop, category):
        raise CatalogCollectionError(
            f"run {run_id} collects {run['shop']} {run['category']}, not {shop} {category}")
    if run["status"] == "completed":
        raise CatalogCollectionError(f"run {run_id} is already completed")
    if run["status"] == "failed":
        store.reopen_run(database_url, run_id)
    return run_id


def check_response(response, url):
    if response.status_code != 200:
        detail = ""
        if response.is_redirect:
            detail = f" redirect to {response.headers.get('Location')!r}"
        elif response.status_code == 429:
            detail = f" Retry-After {response.headers.get('Retry-After')!r}"
        raise CatalogCollectionError(f"HTTP {response.status_code}{detail}")
    content_type = response.headers.get("Content-Type", "")
    if not content_type.startswith("text/html"):
        raise CatalogCollectionError(f"content type {content_type!r} is not text/html")
    if not response.content:
        raise CatalogCollectionError("empty response body")


def save_evidence(evidence_dir, run_id, page_number, url, response, observed_at, error):
    """Write the failing response body and its metadata; returns the body path."""
    directory = Path(evidence_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"run{run_id}-page{page_number}-{observed_at.strftime('%Y%m%dT%H%M%S%fZ')}"
    body = response.content
    metadata = {"run_id": run_id, "page_number": page_number, "request_url": url,
                "status_code": response.status_code, "content_type": response.headers.get("Content-Type"),
                "observed_at": observed_at.isoformat(), "sha256": hashlib.sha256(body).hexdigest(),
                "bytes": len(body), "error": error}
    body_path = directory / f"{stem}.body"
    with open(body_path, "xb") as file:
        file.write(body)
    with open(directory / f"{stem}.json", "x", encoding="utf-8") as file:
        json.dump(metadata, file, ensure_ascii=False, indent=2)
    return body_path


def record_failure(database_url, run_id, message, cause):
    """fail_run with message, then raise CatalogCollectionError; a failure to record is raised with both causes."""
    try:
        store.fail_run(database_url, run_id, message)
    except Exception as record_error:
        raise CatalogCollectionError(
            f"{message}; recording this failure in run {run_id} also failed:"
            f" {type(record_error).__name__}: {record_error}") from record_error
    raise CatalogCollectionError(message) from cause


def collect_catalog(database_url, shop, category, run_id=None, max_pages=None, delay=1.0, session=None,
                    evidence_dir=None):
    """Collect missing pages of one category run and complete it when all are stored; returns store.get_run.

    Once the run is open, any failure (reading its state, a page request, parse or store, or completion) is
    recorded on the run by record_failure; invalid arguments and a run that may not be resumed are rejected
    before the run is touched."""
    check_arguments(shop, category, run_id, max_pages, delay)
    run_id = open_run(database_url, shop, category, run_id)
    owns_session = session is None
    if owns_session:
        session = requests.Session()
    stage = "reading the run state"
    response = None
    observed_at = None
    try:
        run = store.get_run(database_url, run_id)
        stored_pages = set(run["stored_pages"])
        total_pages, total_products = run["total_pages"], run["total_products"]
        product_count = run["product_count"]
        page_size = None  # Cardmoa pages after the first are parsed with the product count of page 1
        requested = 0
        last_start = None
        page_number = 1
        while total_pages is None or page_number <= total_pages:
            if page_number in stored_pages:
                page_number += 1
                continue
            if max_pages is not None and requested == max_pages:
                break
            response = None
            observed_at = None
            if shop == "cardmoa" and page_number > 1 and page_size is None:
                stage = f"page {page_number}: reading the stored product count of page 1"
                page_size = store.page_product_count(database_url, run_id, 1)
            stage = f"page {page_number}: building its URL"
            url = parser.page_url(shop, category, page_number, total_products)
            stage = f"page {page_number} {url}"
            if last_start is not None:
                wait = last_start + delay - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
            last_start = time.monotonic()
            requested += 1
            response = session.get(url, timeout=TIMEOUT_SECONDS, allow_redirects=False,
                                   headers={"User-Agent": USER_AGENT})
            observed_at = datetime.now(timezone.utc)
            check_response(response, url)
            if shop == "cardmoa":
                parsed = parser.parse_cardmoa_page(response.content, url, page_size=page_size)
            else:
                parsed = parser.parse_tcgshop_page(response.content, url)
            page = {key: parsed[key] for key in STORED_PARSED_KEYS}
            inserted = store.store_page(database_url, run_id, page_number, url, observed_at, response.content, page)
            response = None  # stored: later failures are not failures of this response

            if page_number == 1:
                page_size = parsed["page_size"]
            total_pages, total_products = parsed["total_pages"], parsed["total_products"]
            stored_pages.add(page_number)
            product_count += inserted
            priced = sum(product["price_krw"] is not None for product in parsed["products"])
            print(f"run {run_id} {shop}/{category} page {page_number}/{total_pages}: {inserted} products"
                  f" ({priced} priced, {len(parsed['products']) - priced} without price);"
                  f" stored {len(stored_pages)}/{total_pages} pages, {product_count}/{total_products} products",
                  file=sys.stderr, flush=True)
            page_number += 1

        if total_pages is not None and len(stored_pages) == total_pages:
            stage = "completion"
            store.complete_run(database_url, run_id)
        stage = "reading the run summary"
        return store.get_run(database_url, run_id)
    except Exception as error:
        message = f"{stage}: {type(error).__name__}: {error}"
        if evidence_dir is not None and response is not None:
            try:
                evidence_path = save_evidence(evidence_dir, run_id, page_number, url, response, observed_at, message)
                message += f"; response saved to {evidence_path}"
            except OSError as evidence_error:
                message += f"; saving the response to {evidence_dir} failed: {evidence_error}"
        record_failure(database_url, run_id, message, error)
    finally:
        if owns_session:
            session.close()


def main():
    arguments = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = arguments.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create the shop_catalog_* tables (safe to repeat)")
    collect_command = commands.add_parser("collect", help="collect (or resume) one category run")
    collect_command.add_argument("--shop", required=True, choices=sorted({shop for shop, _ in parser.CATEGORIES}))
    collect_command.add_argument("--category", required=True)
    collect_command.add_argument("--run-id", type=int)
    collect_command.add_argument("--max-pages", type=int)
    collect_command.add_argument("--delay", type=float, default=1.0)
    collect_command.add_argument("--evidence-dir")
    status_command = commands.add_parser("status", help="show one run")
    status_command.add_argument("--run-id", type=int, required=True)
    options = arguments.parse_args()
    if options.command == "collect":
        try:
            check_arguments(options.shop, options.category, options.run_id, options.max_pages, options.delay)
        except CatalogCollectionError as error:
            arguments.error(str(error))  # exits with status 2 like other invalid arguments

    try:
        database_url = required_env("DATABASE_URL")
        if options.command == "init":
            store.init_db(database_url)
            result = {"initialized": True}
        elif options.command == "collect":
            result = collect_catalog(database_url, options.shop, options.category, run_id=options.run_id,
                                     max_pages=options.max_pages, delay=options.delay,
                                     evidence_dir=options.evidence_dir)
        else:
            result = store.get_run(database_url, options.run_id)
    except PriceError as error:
        print(f"{options.command} failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if options.command == "collect" and result["status"] != "completed":
        return EXIT_INCOMPLETE
    return 0


if __name__ == "__main__":
    sys.exit(main())
