"""Official OCG rarities (Korean and Japanese editions only) for the official card inventory.

    .venv/Scripts/python -X utf8 rarities.py --snapshot 2026-10-07   # crawl (resumable) + validate + write DB

Reading (used by the app): get_rarities(cid) reads data/official_cards/official_cards.sqlite and never touches
the network. A CID outside the inventory snapshot raises RarityError: there is no fetch-on-demand, refresh by
re-running inventory.py and then this command with a new snapshot.

Enrichment (this command), all from the official card database, request_locale ko and ja only (never en/TCG):
1. Product index: card_list.action lists every product (pid) of the locale.
2. Product pages: card_search.action?ope=1&sess=1&pid=<pid>&rp=99999 lists every card of the product, one row per
   card with all of its rarities in that product. The page's "N cards" total must equal its rows.
   ~2,100 product pages replace ~28,000 card detail pages.
3. Detail checks (card_search.action?ope=2&cid=<cid>): every card listed in a locale's full list but found in no
   product of that locale must show an empty print list there (confirmed "no_printing"); a fixed random sample
   of printed cards plus SAMPLE_ALWAYS must list exactly the (product, rarity) pairs the product pages gave.
Every fetched page is checkpointed as parsed JSON plus the html sha256 under
data/official_cards/rarity_snapshots/<snapshot>/, so rerunning the same snapshot resumes, and runs offline once
complete. The rarity tables are written into official_cards.sqlite (existing tables untouched) only when the whole
crawl validated without error; otherwise the report lists the errors and the command exits non-zero.
"""

import argparse
import hashlib
import json
import random
import re
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

import catalog

OFFICIAL_DIR = Path("data/official_cards")
DATABASE_PATH = OFFICIAL_DIR / "official_cards.sqlite"
LOCALES = ("ko", "ja")
PRODUCT_INDEX_URL = catalog.SITE + "/yugiohdb/card_list.action"
WORKERS = 3
SAMPLE_PER_LOCALE = 100
SAMPLE_SEED = 20261007
SAMPLE_ALWAYS = (16537,)  # Evil★Twin's Trouble Sunny: KO/JA prints in several products and rarities

# Rarity key -> (official rid, official code printed by the {"ko", "ja"} site, Korean display label).
# The rid identifies the finish: the site prints one code for several finishes ("SE" is rid 5, 43 and 50), so a
# variant rid gets the key "<code>@<rid>". A locale code of None means that site has never printed the rid.
# Korean labels are the KO site's own labels; for JA-only rids they translate the JA label (pattern of the KO
# site's 패러렐(...)/밀레니엄(...) labels).
RARITIES = {
    "N": (1, {"ko": "N", "ja": "N"}, "노멀"),
    "P": (9, {"ko": "P", "ja": "P"}, "패러렐(노멀)"),
    "KC": (21, {"ko": None, "ja": "KC"}, "KC(노멀)"),
    "M": (26, {"ko": "M", "ja": "M"}, "밀레니엄"),
    "R": (2, {"ko": "R", "ja": "R"}, "레어"),
    "P+R": (10, {"ko": None, "ja": "P+R"}, "패러렐(레어)"),
    "KC+R": (22, {"ko": None, "ja": "KC+R"}, "KC(레어)"),
    "SR": (3, {"ko": "SR", "ja": "SR"}, "슈퍼 레어"),
    "P+SR": (12, {"ko": "P+SR", "ja": "P+SR"}, "패러렐(슈퍼 레어)"),
    "M+SR": (28, {"ko": "M+SR", "ja": "M+SR"}, "밀레니엄(슈퍼 레어)"),
    "GR": (8, {"ko": "GR", "ja": "GR"}, "골드 레어"),
    "M+GR": (31, {"ko": "M+GR", "ja": "M+GR"}, "밀레니엄(골드 레어)"),
    "UR": (4, {"ko": "UR", "ja": "UR"}, "울트라 레어"),
    "UR@41": (41, {"ko": None, "ja": "UR"}, "울트라 레어（RED Ver.）"),
    "UR@42": (42, {"ko": None, "ja": "UR"}, "울트라 레어（BLUE Ver.）"),
    "UR@52": (52, {"ko": None, "ja": "UR"}, "울트라 레어（SPECIAL PURPLE Ver.）"),
    "UR@57": (57, {"ko": None, "ja": "UR"}, "울트라 레어（SUMI-E BLACK Ver.）"),
    "P+UR": (11, {"ko": "P+UR", "ja": "P+UR"}, "패러렐(울트라 레어)"),
    "KC+UR": (24, {"ko": None, "ja": "KC+UR"}, "KC(울트라 레어)"),
    "M+UR": (29, {"ko": "M+UR", "ja": "M+UR"}, "밀레니엄(울트라 레어)"),
    "SE": (5, {"ko": "SE", "ja": "SE"}, "시크릿 레어"),
    "SE@43": (43, {"ko": "SE", "ja": "SE"}, "시크릿 레어（SPECIAL BLUE Ver.）"),
    "SE@50": (50, {"ko": "SE", "ja": "SE"}, "시크릿 레어（SPECIAL RED Ver.）"),
    "P+SE": (34, {"ko": "P+SE", "ja": "P+SE"}, "패러렐(시크릿 레어)"),
    "M+SE": (30, {"ko": "M+SE", "ja": "M+SE"}, "밀레니엄(시크릿 레어)"),
    "GSE": (14, {"ko": "GSE", "ja": "GSE"}, "골드 시크릿 레어"),
    "PG": (38, {"ko": "PG", "ja": "PG"}, "프리미엄 골드 레어"),
    "CR": (16, {"ko": "CR", "ja": "CR"}, "컬렉터즈 레어"),
    "UL": (6, {"ko": "UL", "ja": "UL"}, "얼티미트 레어"),
    "HR": (7, {"ko": "HR", "ja": "HR"}, "홀로그래픽 레어"),
    "P+HR": (33, {"ko": None, "ja": "P+HR"}, "패러렐(홀로그래픽 레어)"),
    "EXSE": (15, {"ko": "EXSE", "ja": "EXSE"}, "엑스트라 시크릿 레어"),
    "P+EXSE": (32, {"ko": "P+ES", "ja": "P+EXSE"}, "패러렐(엑스트라 시크릿 레어)"),
    "20th SE": (35, {"ko": None, "ja": "20th SE"}, "20th 시크릿 레어"),
    "PSE": (36, {"ko": "PSE", "ja": "PSE"}, "프리즈마틱 시크릿 레어"),
    "PSE@58": (58, {"ko": None, "ja": "PSE"}, "프리즈마틱 시크릿 레어（SUMI-E BLACK Ver.）"),
    "QCSE": (51, {"ko": "QCSE", "ja": "QCSE"}, "쿼터 센추리 시크릿 레어"),
    "QCSE@53": (53, {"ko": None, "ja": "QCSE"}, "쿼터 센추리 시크릿 레어（TOKYO DOME GREEN Ver.）"),
    "QCSE@54": (54, {"ko": "QCSE", "ja": "QCSE"}, "쿼터 센추리 시크릿 레어（SPECIAL Ver.）"),
    "GMR": (56, {"ko": "GMR", "ja": "GMR"}, "그랜드마스터 레어 사양"),
    "10000 SE": (37, {"ko": "10000 SE", "ja": "10000 SE"}, "10000 시크릿 레어"),
}
# The KO site's label where it differs from the display label (its typo "BULE").
KO_SITE_LABELS = {43: "시크릿 레어（SPECIAL BULE Ver.）"}
# Increasing order, lowest first: the order of RARITIES. Tiers follow the official N < R < SR < UR < SE ladder
# (GR placed between SR and UR, HR above UL); within a tier and for the special finishes after SE the order is
# this app's convention (base finish first, then KC/Millennium/Parallel/colour variants), not price.
RARITY_ORDER = tuple(RARITIES)
RARITY_LABELS = {key: label for key, (_, _, label) in RARITIES.items()}
RARITY_KEYS_BY_RID = {rid: key for key, (rid, _, _) in RARITIES.items()}

PRODUCT_TOTAL_PATTERNS = {"ko": re.compile(r"전\s*([\d,]+)\s*장"), "ja": re.compile(r"全\s*([\d,]+)\s*枚")}
RELEASE_DATE_PATTERN = re.compile(r"\d{4}/\d{2}/\d{2}")
DETAIL_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
SCHEMA = """
CREATE TABLE rarity_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE rarity_codes (key TEXT PRIMARY KEY, rid INTEGER NOT NULL UNIQUE, codes TEXT NOT NULL,  -- JSON {locale: printed code}
    label_ko TEXT NOT NULL, sort_order INTEGER NOT NULL UNIQUE);
CREATE TABLE rarity_products (locale TEXT NOT NULL CHECK (locale IN ('ko', 'ja')), pid INTEGER NOT NULL,
    name TEXT NOT NULL, release_date TEXT NOT NULL, url TEXT NOT NULL, card_count INTEGER NOT NULL,
    fetched_at TEXT NOT NULL, html_sha256 TEXT NOT NULL, PRIMARY KEY (locale, pid));
CREATE TABLE rarity_prints (locale TEXT NOT NULL, pid INTEGER NOT NULL, cid INTEGER NOT NULL REFERENCES cards (cid),
    rid INTEGER NOT NULL REFERENCES rarity_codes (rid), code TEXT NOT NULL, label_local TEXT NOT NULL,
    PRIMARY KEY (locale, pid, cid, rid), FOREIGN KEY (locale, pid) REFERENCES rarity_products (locale, pid));
CREATE TABLE rarity_coverage (locale TEXT NOT NULL CHECK (locale IN ('ko', 'ja')),
    cid INTEGER NOT NULL REFERENCES cards (cid),
    status TEXT NOT NULL CHECK (status IN ('printed', 'no_printing')),
    evidence_url TEXT, evidence_sha256 TEXT,   -- the detail page with an empty print list, for 'no_printing'
    PRIMARY KEY (locale, cid), CHECK ((status = 'no_printing') = (evidence_url IS NOT NULL)));
"""
RARITY_TABLES = ("rarity_meta", "rarity_codes", "rarity_products", "rarity_prints", "rarity_coverage")


class RarityError(Exception):
    pass


class RarityUnavailable(RarityError):
    """The card is in the inventory but has no Korean or Japanese printing."""


# ---------------------------------------------------------------- reading

def get_rarities(cid, database_path=DATABASE_PATH, *, locale=None):
    """Rarity keys (see RARITIES) of every KO/JA printing of cid, unique and in RARITY_ORDER (lowest first).

    With locale ('ko' or 'ja') only that edition's printings count, after the same validation of the whole card;
    a card without any printing in that edition raises RarityUnavailable.
    """
    if type(cid) is not int or cid <= 0:
        raise RarityError(f"CID must be a positive int, got {cid!r}")
    if locale is not None and locale not in LOCALES:
        raise ValueError(f"locale must be None or one of {LOCALES}, got {locale!r}")
    database_path = Path(database_path)
    if not database_path.is_file():
        raise RarityError(f"{database_path} does not exist; run inventory.py and rarities.py first")
    with closing(sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not set(RARITY_TABLES) <= tables:
            raise RarityError(f"{database_path} has no rarity data; run rarities.py --snapshot <snapshot>")
        status = connection.execute("SELECT value FROM rarity_meta WHERE key = 'status'").fetchone()
        if status is None or json.loads(status[0]) != "complete":
            raise RarityError(f"{database_path} rarity data is not marked complete (status {status})")
        names = connection.execute("SELECT name_ko, name_ja FROM cards WHERE cid = ?", (cid,)).fetchone()
        if names is None:
            raise RarityError(f"CID {cid} is not in the official inventory snapshot; re-run inventory.py and "
                              "rarities.py with a new snapshot to include it")
        coverage = dict(connection.execute("SELECT locale, status FROM rarity_coverage WHERE cid = ?", (cid,)))
        printed_codes = connection.execute("SELECT DISTINCT locale, rid, code FROM rarity_prints WHERE cid = ?",
                                           (cid,)).fetchall()
    prints = {(locale, rid) for locale, rid, _ in printed_codes}

    listed = {locale for locale, name in zip(("ko", "ja"), names) if name is not None}
    if set(coverage) != listed:
        raise RarityError(f"CID {cid} is listed in {sorted(listed)} but has rarity coverage for {sorted(coverage)}")
    foreign = sorted({locale for locale, _ in prints} - listed)
    if foreign:
        raise RarityError(f"CID {cid} has prints from locales {foreign} outside its KO/JA listings {sorted(listed)}")
    if not listed:
        raise RarityUnavailable(f"CID {cid} is not listed in the Korean or Japanese official card list")
    for listed_locale in sorted(listed):
        has_prints = any(print_locale == listed_locale for print_locale, _ in prints)
        if coverage[listed_locale] not in ("printed", "no_printing")                 or (coverage[listed_locale] == "printed") != has_prints:
            raise RarityError(f"CID {cid} {listed_locale} coverage is {coverage[listed_locale]!r} but it has "
                              f"{'some' if has_prints else 'no'} {listed_locale} prints")
    unknown = sorted({rid for _, rid in prints} - set(RARITY_KEYS_BY_RID))
    if unknown:
        raise RarityError(f"CID {cid} has rarity rids unknown to this app: {unknown}")
    for print_locale, rid, code in printed_codes:
        expected = RARITIES[RARITY_KEYS_BY_RID[rid]][1][print_locale]
        if code != expected:
            raise RarityError(f"CID {cid} {print_locale} print of rid {rid} has code {code!r}, RARITIES expects {expected!r}")
    if not prints:
        raise RarityUnavailable(f"CID {cid} has no Korean or Japanese printing (listed in {sorted(listed)})")
    if locale is not None:
        prints = {(print_locale, rid) for print_locale, rid in prints if print_locale == locale}
        if not prints:
            raise RarityUnavailable(f"CID {cid} has no {locale} printing")
    return sorted({RARITY_KEYS_BY_RID[rid] for _, rid in prints}, key=RARITY_ORDER.index)


# ---------------------------------------------------------------- parsing

def text_of(element):
    return " ".join(element.get_text().split())


def compact(text):
    return "".join(text.split())


def parse_rarity_icons(container, where):
    """[{"code", "rid", "label"}] of the .lr_icon rarity badges inside container."""
    rarities = []
    for icon in container.select(".lr_icon"):
        rids = [int(c[4:]) for c in icon.get("class", []) if re.fullmatch(r"rid_\d+", c)]
        code, label = icon.select_one("p"), icon.select_one("span")
        if len(rids) != 1 or code is None or label is None or not text_of(code) or not text_of(label):
            raise RarityError(f"{where}: malformed rarity badge {str(icon)[:200]!r}")
        rarities.append({"code": text_of(code), "rid": rids[0], "label": text_of(label)})
    if not rarities:
        raise RarityError(f"{where}: no rarity badge")
    duplicates = sorted(rid for rid, count in Counter(r["rid"] for r in rarities).items() if count > 1)
    if duplicates:
        raise RarityError(f"{where}: rarity badge rid {duplicates} listed twice")
    return rarities


def parse_product_index(html, locale):
    """[{"pid", "name", "release_date"}] of a locale's card_list.action page."""
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("#update_list .t_row")
    if not rows:
        raise RarityError(f"{locale} product index has no #update_list .t_row")
    products = {}
    for row in rows:
        link, name, date = row.select_one("input.link_value"), row.select_one(".main p"), row.select_one(".time")
        match = re.fullmatch(r"/yugiohdb/card_search\.action\?ope=1&sess=1&pid=(\d+)&rp=99999",
                             link.get("value", "") if link else "")
        if match is None or name is None or not text_of(name) or date is None:
            raise RarityError(f"{locale} product index row is malformed: {str(row)[:300]!r}")
        if not RELEASE_DATE_PATTERN.fullmatch(text_of(date)):
            raise RarityError(f"{locale} product index row {match.group(1)} has release date {text_of(date)!r}")
        product = {"pid": int(match.group(1)), "name": text_of(name), "release_date": text_of(date)}
        if products.setdefault(product["pid"], product) != product:
            raise RarityError(f"{locale} product index lists pid {product['pid']} twice with different data")
    return list(products.values())


def parse_product_page(html, locale, product):
    """{"total", "rows": [{"cid", "name", "rarities"}]} of one product page, validated against the index."""
    where = f"{locale} pid {product['pid']}"
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text().split(" | ")[0] if soup.title else ""
    if compact(title) != compact(product["name"]):
        raise RarityError(f"{where}: page title {title!r} is not the indexed product {product['name']!r}")
    total_element = soup.select_one(".sort_set .text")
    match = PRODUCT_TOTAL_PATTERNS[locale].fullmatch(text_of(total_element)) if total_element else None
    if match is None:
        raise RarityError(f"{where}: no {locale} card total in .sort_set .text")
    rows = []
    for row in soup.select("#card_list .t_row"):
        cid_input, name = row.select_one("input.cid"), row.select_one(".card_name")
        link = row.select_one("input.link_value")
        link_match = re.fullmatch(r"/yugiohdb/card_search\.action\?ope=2&cid=(\d+)", link.get("value", "") if link else "")
        if cid_input is None or not cid_input.get("value", "").isdigit() or link_match is None \
                or link_match.group(1) != cid_input["value"] or name is None or not text_of(name):
            raise RarityError(f"{where}: malformed card row {str(row)[:300]!r}")
        cid = int(cid_input["value"])
        badges = row.select(".icon.rarity")
        if len(badges) != 1:
            raise RarityError(f"{where}: card {cid} row has {len(badges)} .icon.rarity blocks, expected 1")
        rows.append({"cid": cid, "name": text_of(name), "rarities": parse_rarity_icons(badges[0], f"{where} cid {cid}")})
    total = int(match.group(1).replace(",", ""))
    if len(rows) != total:
        raise RarityError(f"{where}: {len(rows)} card rows but the page reports {total}")
    duplicates = sorted(cid for cid, count in Counter(row["cid"] for row in rows).items() if count > 1)
    if duplicates:
        raise RarityError(f"{where}: cids listed twice: {duplicates}")
    return {"total": total, "rows": rows}


def parse_detail_prints(html, locale, cid, expected_name):
    """[{"pid", "card_number", "release_date", "code", "rid", "label"}] of a card detail page's print list."""
    where = f"{locale} detail cid {cid}"
    try:
        name, _ = catalog.parse_detail_page(html)
    except catalog.CatalogError as error:
        raise RarityError(f"{where}: {error}") from error
    if name is None or catalog.normalize_name(name) != catalog.normalize_name(expected_name):
        raise RarityError(f"{where}: detail name {name!r} is not the listed name {expected_name!r}")
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one("#update_list .t_body")
    if body is None:
        raise RarityError(f"{where}: no #update_list .t_body print list")
    prints = []
    for row in body.select(".t_row"):
        link, date, number = row.select_one("input.link_value"), row.select_one(".time"), row.select_one(".card_number")
        match = re.fullmatch(r"/yugiohdb/card_search\.action\?ope=1&sess=1&pid=(\d+)&rp=99999",
                             link.get("value", "") if link else "")
        if match is None or date is None or number is None or not DETAIL_DATE_PATTERN.fullmatch(text_of(date)):
            raise RarityError(f"{where}: malformed print row {str(row)[:300]!r}")
        badges = row.select(".icon.rarity")
        if len(badges) != 1:
            raise RarityError(f"{where}: print row of pid {match.group(1)} has {len(badges)} .icon.rarity blocks, expected 1")
        for rarity in parse_rarity_icons(badges[0], f"{where} pid {match.group(1)}"):
            prints.append({"pid": int(match.group(1)), "card_number": text_of(number), "release_date": text_of(date),
                           **rarity})
    return prints


# ---------------------------------------------------------------- crawling

_local = threading.local()


def session():
    if not hasattr(_local, "session"):
        _local.session = catalog.new_session()
    return _local.session


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(message):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_json(path, data):
    catalog.write_atomic(Path(path), json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))


def product_url(locale, pid):
    return f"{catalog.SEARCH_URL}?ope=1&sess=1&pid={pid}&rp=99999&request_locale={locale}"


def is_text(value):
    return isinstance(value, str) and value.strip() != ""


def is_id(value):
    return type(value) is int and value > 0


def is_rarity(entry):
    return isinstance(entry, dict) and is_text(entry.get("code")) and is_id(entry.get("rid")) and is_text(entry.get("label"))


def validate_record(kind, record):
    """Problems with a fetched or cached checkpoint record of kind "index", "product" or "detail" ([] = valid)."""
    problems = []
    if not (is_text(record.get("url")) and record["url"].startswith(catalog.SITE)):
        problems.append(f"url {record.get('url')!r} is not an official site URL")
    if not is_text(record.get("fetched_at")):
        problems.append("no fetched_at")
    if not (isinstance(record.get("html_sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", record["html_sha256"])):
        problems.append("html_sha256 is not a sha256 hex digest")
    if kind == "index":
        products = record.get("products")
        if not isinstance(products, list) or not products:
            return problems + ["products is not a non-empty list"]
        for product in products:
            if not (isinstance(product, dict) and is_id(product.get("pid")) and is_text(product.get("name"))
                    and isinstance(product.get("release_date"), str)
                    and RELEASE_DATE_PATTERN.fullmatch(product["release_date"])):
                problems.append(f"malformed product {product!r}")
        pids = [product.get("pid") for product in products if isinstance(product, dict)]
        if len(set(pids)) != len(pids):
            problems.append("a pid is listed twice")
    elif kind == "product":
        rows = record.get("rows")
        if not isinstance(rows, list) or type(record.get("total")) is not int or record["total"] != len(rows):
            return problems + [f"total {record.get('total')!r} does not equal the number of rows"]
        for row in rows:
            rarities = row.get("rarities") if isinstance(row, dict) else None
            if not (is_id(row.get("cid")) and is_text(row.get("name")) and isinstance(rarities, list) and rarities
                    and all(is_rarity(entry) for entry in rarities)
                    and len({entry["rid"] for entry in rarities}) == len(rarities)):
                problems.append(f"malformed card row {row!r}")
        cids = [row.get("cid") for row in rows if isinstance(row, dict)]
        if len(set(cids)) != len(cids):
            problems.append("a cid is listed twice")
    elif kind == "detail":
        prints = record.get("prints")
        if not isinstance(prints, list):
            return problems + ["prints is not a list"]
        for entry in prints:
            # card_number may be "": the official page lists no set number for e.g. 1999-2000 JA products.
            if not (is_rarity(entry) and is_id(entry.get("pid")) and isinstance(entry.get("card_number"), str)
                    and isinstance(entry.get("release_date"), str)
                    and DETAIL_DATE_PATTERN.fullmatch(entry["release_date"])):
                problems.append(f"malformed print {entry!r}")
    else:
        raise ValueError(f"unknown checkpoint kind {kind!r}")
    return problems


def checkpointed(path, kind, identity, fetch):
    """The checkpoint at path, else fetch() -> record, checkpointed. Identity fields must match and the record must
    pass validate_record() either way: a corrupted checkpoint fails, it is never silently refetched."""
    cached = path.exists()
    record = json.loads(path.read_text(encoding="utf-8")) if cached else {**identity, **fetch()}
    if not isinstance(record, dict) or {key: record.get(key) for key in identity} != identity:
        raise RarityError(f"{path} is not the checkpoint of {identity}")
    problems = validate_record(kind, record)
    if problems:
        state = "cached checkpoint" if cached else "fetched record"
        raise RarityError(f"{path} {state} is invalid (move it aside to refetch): {'; '.join(problems[:5])}")
    if not cached:
        write_json(path, record)
    return record


def load_index(snapshot_dir, locale):
    def fetch():
        url = f"{PRODUCT_INDEX_URL}?request_locale={locale}"
        html = catalog.fetch_html(session(), url)
        return {"url": url, "fetched_at": utc_now(), "html_sha256": sha256_text(html),
                "products": parse_product_index(html, locale)}
    return checkpointed(snapshot_dir / locale / "index.json", "index", {"locale": locale}, fetch)


def load_product(snapshot_dir, locale, product):
    def fetch():
        url = product_url(locale, product["pid"])
        html = catalog.fetch_html(session(), url)
        return {"url": url, "fetched_at": utc_now(), "html_sha256": sha256_text(html),
                **parse_product_page(html, locale, product)}
    identity = {"locale": locale, "pid": product["pid"], "name": product["name"]}
    return checkpointed(snapshot_dir / locale / "products" / f"{product['pid']}.json", "product", identity, fetch)


def load_detail(snapshot_dir, locale, cid, name):
    def fetch():
        url = catalog.detail_url(cid, locale)
        html = catalog.fetch_html(session(), url)
        return {"url": url, "fetched_at": utc_now(), "html_sha256": sha256_text(html),
                "prints": parse_detail_prints(html, locale, cid, name)}
    return checkpointed(snapshot_dir / locale / "details" / f"{cid}.json", "detail",
                        {"locale": locale, "cid": cid, "name": name}, fetch)


def run_all(label, jobs):
    """Run (key, callable) jobs with WORKERS threads; returns ({key: result}, [error dicts])."""
    results, errors = {}, []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [(key, pool.submit(job)) for key, job in jobs]
        for number, (key, future) in enumerate(futures, 1):
            try:
                results[key] = future.result()
            except (requests.RequestException, catalog.CatalogError, RarityError, OSError, ValueError) as error:
                # Recorded and fails the run; other jobs still checkpoint so a rerun resumes.
                errors.append({"job": label, "key": list(key), "error": f"{type(error).__name__}: {error}"})
            if number % 200 == 0 or number == len(futures):
                log(f"{label}: {number}/{len(futures)} done, {len(errors)} failed")
    return results, errors


# ---------------------------------------------------------------- building

def load_inventory(connection):
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not {"cards", "meta"} <= tables:
        raise RarityError("official_cards.sqlite has no cards/meta tables; run inventory.py first")
    snapshot = json.loads(connection.execute("SELECT value FROM meta WHERE key = 'snapshot'").fetchone()[0])
    cards = {cid: {"ko": name_ko, "ja": name_ja, "en": name_en}
             for cid, name_ko, name_ja, name_en in connection.execute("SELECT cid, name_ko, name_ja, name_en FROM cards")}
    return snapshot, cards


def enrich(snapshot, official_dir=OFFICIAL_DIR):
    started = time.perf_counter()
    database_path = official_dir / "official_cards.sqlite"
    snapshot_dir = official_dir / "rarity_snapshots" / snapshot
    with closing(sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        inventory_snapshot, cards = load_inventory(connection)
    errors = []

    # 1-2. Product index and product pages.
    indexes = {}
    for locale in LOCALES:
        indexes[locale] = load_index(snapshot_dir, locale)
        log(f"{locale}: {len(indexes[locale]['products'])} products in the index")
    product_jobs = [((locale, p["pid"]), lambda l=locale, p=p: load_product(snapshot_dir, l, p))
                    for locale in LOCALES for p in indexes[locale]["products"]]
    products, product_errors = run_all("products", product_jobs)
    errors += product_errors

    # Rarity codes must be consistent and known; rows must name inventory cards listed in that locale.
    rid_seen = defaultdict(set)   # (locale, rid) -> {(code, label)}
    prints = defaultdict(set)     # (locale, cid) -> {(pid, code)}
    for (locale, pid), record in sorted(products.items()):
        for row in record["rows"]:
            if row["cid"] not in cards:
                errors.append({"job": "validate", "key": [locale, pid, row["cid"]],
                               "error": f"product row cid {row['cid']} {row['name']!r} is not in the inventory"})
                continue
            if cards[row["cid"]][locale] is None:
                errors.append({"job": "validate", "key": [locale, pid, row["cid"]],
                               "error": f"cid {row['cid']} is in a {locale} product but not in the {locale} list"})
            for rarity in row["rarities"]:
                rid_seen[(locale, rarity["rid"])].add((rarity["code"], rarity["label"]))
                prints[(locale, row["cid"])].add((pid, rarity["rid"]))
    codes_found = {f"{locale} rid {rid}": sorted(variants) for (locale, rid), variants in sorted(rid_seen.items())}
    for (locale, rid), variants in sorted(rid_seen.items()):
        key = RARITY_KEYS_BY_RID.get(rid)
        if key is None:
            errors.append({"job": "validate", "key": [locale, rid], "error": f"unknown rarity rid {rid}: {sorted(variants)}"})
            continue
        expected_code = RARITIES[key][1][locale]
        expected_label = KO_SITE_LABELS.get(rid, RARITIES[key][2]) if locale == "ko" else None
        for code, label in variants:
            if code != expected_code or (expected_label is not None and label != expected_label):
                errors.append({"job": "validate", "key": [locale, rid],
                               "error": f"rid {rid} printed as {code!r} / {label!r}; RARITIES expects {expected_code!r}"
                                        + (f" / {expected_label!r}" if expected_label else "")})
        if len({label for _, label in variants}) != 1:
            errors.append({"job": "validate", "key": [locale, rid], "error": f"rid {rid} has several labels {sorted(variants)}"})

    # 3. Detail checks: unprinted listed cards, and a fixed random sample of printed cards.
    listed = {locale: sorted(cid for cid, names in cards.items() if names[locale] is not None) for locale in LOCALES}
    unprinted = {locale: [cid for cid in listed[locale] if (locale, cid) not in prints] for locale in LOCALES}
    sampled = {}
    for locale in LOCALES:
        printed = [cid for cid in listed[locale] if (locale, cid) in prints]
        sampled[locale] = sorted(set(random.Random(f"{SAMPLE_SEED}-{locale}").sample(printed, SAMPLE_PER_LOCALE))
                                 | {cid for cid in SAMPLE_ALWAYS if (locale, cid) in prints})
    detail_jobs = [((locale, cid), lambda l=locale, c=cid: load_detail(snapshot_dir, l, c, cards[c][l]))
                   for locale in LOCALES for cid in sorted(set(unprinted[locale]) | set(sampled[locale]))]
    details, detail_errors = run_all("details", detail_jobs)
    errors += detail_errors
    for (locale, cid), record in sorted(details.items()):
        from_detail = {(p["pid"], p["rid"]) for p in record["prints"]}
        from_products = prints.get((locale, cid), set())
        if from_detail != from_products:
            errors.append({"job": "verify", "key": [locale, cid],
                           "error": f"detail page prints {sorted(from_detail)} differ from product pages "
                                    f"{sorted(from_products)} ({record['url']})"})

    report = {"snapshot": snapshot, "inventory_snapshot": inventory_snapshot, "finished_at": utc_now(),
              "status": "complete" if not errors else "incomplete", "locales": {}, "rarity_codes_found": codes_found,
              "detail_sample": {locale: sampled[locale] for locale in LOCALES}, "errors": errors}
    for locale in LOCALES:
        locale_products = [products[(locale, p["pid"])] for p in indexes[locale]["products"] if (locale, p["pid"]) in products]
        report["locales"][locale] = {
            "products_indexed": len(indexes[locale]["products"]), "products_fetched": len(locale_products),
            "product_rows": sum(len(p["rows"]) for p in locale_products),
            "listed_cids": len(listed[locale]), "printed_cids": len(listed[locale]) - len(unprinted[locale]),
            "no_printing_cids": unprinted[locale], "detail_pages_checked": sum(1 for l, _ in details if l == locale),
            "prints": sum(len(v) for (l, _), v in prints.items() if l == locale),
            "rarity_counts": {RARITY_KEYS_BY_RID.get(rid, f"unknown rid {rid}"): count for rid, count in sorted(
                Counter(rid for (l, _), v in prints.items() if l == locale for _, rid in v).items())}}
    both = set(listed["ko"]) | set(listed["ja"])
    report["cids"] = {"inventory": len(cards), "listed_ko_or_ja": len(both),
                      "not_listed_ko_or_ja": sorted(set(cards) - both),
                      "with_rarity": len({cid for (_, cid) in prints}),
                      "listed_without_any_printing": sorted(cid for cid in both if all((l, cid) not in prints for l in LOCALES))}
    report["seconds"] = round(time.perf_counter() - started, 1)
    if not errors:
        write_rarity_tables(database_path, report, indexes, products, prints, details, unprinted)
        report["database"] = update_manifest(official_dir, database_path, report)
    write_json(official_dir / "rarity_report.json", report)
    write_report_text(official_dir / "rarity_report.txt", report, cards)
    return report


def write_rarity_tables(database_path, report, indexes, products, prints, details, unprinted):
    """Replace the rarity tables in one transaction; the inventory's own tables are not touched."""
    connection = sqlite3.connect(database_path)
    try:
        with connection:
            drops = "".join(f"DROP TABLE IF EXISTS {table};" for table in RARITY_TABLES)
            connection.executescript("BEGIN;" + drops + SCHEMA)  # left open: committed with the inserts below
            meta = {"status": report["status"], "snapshot": report["snapshot"],
                    "inventory_snapshot": report["inventory_snapshot"], "built_at": report["finished_at"],
                    "source": f"{PRODUCT_INDEX_URL} + {catalog.SEARCH_URL}?ope=1&sess=1&pid=<pid>&rp=99999",
                    "locales": list(LOCALES), "order_rule": "rarity_codes.sort_order = RARITY_ORDER in rarities.py: N<R<SR<UR<SE "
                                                            "official tiers; other finishes placed by app convention, "
                                                            "not an official ranking or price"}
            connection.executemany("INSERT INTO rarity_meta VALUES (?, ?)",
                                   [(k, json.dumps(v, ensure_ascii=False)) for k, v in meta.items()])
            connection.executemany("INSERT INTO rarity_codes VALUES (?, ?, ?, ?, ?)",
                                   [(key, RARITIES[key][0], json.dumps(RARITIES[key][1]), RARITIES[key][2],
                                     RARITY_ORDER.index(key)) for key in RARITY_ORDER])
            for locale in LOCALES:
                for product in indexes[locale]["products"]:
                    record = products[(locale, product["pid"])]
                    connection.execute("INSERT INTO rarity_products VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                       (locale, product["pid"], product["name"], product["release_date"], record["url"],
                                        record["total"], record["fetched_at"], record["html_sha256"]))
                    for row in record["rows"]:
                        connection.executemany("INSERT INTO rarity_prints VALUES (?, ?, ?, ?, ?, ?)",
                                               [(locale, product["pid"], row["cid"], r["rid"], r["code"], r["label"])
                                                for r in row["rarities"]])
                connection.executemany("INSERT INTO rarity_coverage VALUES (?, ?, 'printed', NULL, NULL)",
                                       [(locale, cid) for (l, cid) in sorted(prints) if l == locale])
                connection.executemany("INSERT INTO rarity_coverage VALUES (?, ?, 'no_printing', ?, ?)",
                                       [(locale, cid, details[(locale, cid)]["url"], details[(locale, cid)]["html_sha256"])
                                        for cid in unprinted[locale]])
            # Inside the transaction: a failure rolls back to the previous rarity tables.
            problems = connection.execute("PRAGMA foreign_key_check").fetchall()
            if problems or connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RarityError(f"{database_path} failed foreign key / integrity check: {problems[:10]}")
    finally:
        connection.close()


def update_manifest(official_dir, database_path, report):
    """Record the new database hash and the rarity summary in the inventory manifest (other keys kept)."""
    manifest_path = official_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    database = {"path": database_path.as_posix(), "sha256": hashlib.sha256(database_path.read_bytes()).hexdigest(),
                "bytes": database_path.stat().st_size}
    manifest["database"] = database
    manifest["rarity"] = {"snapshot": report["snapshot"], "status": report["status"], "built_at": report["finished_at"],
                          "report": (official_dir / "rarity_report.json").as_posix(),
                          "tables": list(RARITY_TABLES), "cids_with_rarity": report["cids"]["with_rarity"]}
    write_json(manifest_path, manifest)
    return database


def write_report_text(path, report, cards):
    def name(cid):
        return cards[cid]["ko"] or cards[cid]["ja"]
    lines = [f"Official KO/JA rarity enrichment: {report['status'].upper()}",
             f"Rarity snapshot {report['snapshot']} on inventory snapshot {report['inventory_snapshot']}, "
             f"finished {report['finished_at']}, {report['seconds']} s",
             f"Inventory {report['cids']['inventory']} CIDs; listed in KO or JA {report['cids']['listed_ko_or_ja']}; "
             f"with rarity {report['cids']['with_rarity']}; listed without any KO/JA printing "
             f"{len(report['cids']['listed_without_any_printing'])}; not listed in KO/JA "
             f"{len(report['cids']['not_listed_ko_or_ja'])}",
             f"Rarity codes found: {report['rarity_codes_found']}", ""]
    for locale, summary in report["locales"].items():
        lines.append(f"[{locale}] products {summary['products_fetched']}/{summary['products_indexed']}, "
                     f"rows {summary['product_rows']}, prints {summary['prints']}, listed {summary['listed_cids']}, "
                     f"printed {summary['printed_cids']}, no printing {len(summary['no_printing_cids'])}, "
                     f"detail pages checked {summary['detail_pages_checked']}")
        lines.append(f"     rarity counts {summary['rarity_counts']}")
    if report["errors"]:
        lines += ["", f"== Errors ({len(report['errors'])}; rerun the same --snapshot to resume) =="]
        lines += [f"- {e['job']} {e['key']}: {e['error']}" for e in report["errors"]]
    lines += ["", "== Listed in KO/JA but no KO/JA printing (confirmed by empty detail print list) =="]
    lines += [f"- CID {cid} {name(cid)}" for cid in report["cids"]["listed_without_any_printing"]]
    lines += ["", "== Not listed in KO or JA (no KO/JA edition; get_rarities raises RarityUnavailable) =="]
    lines += [f"- CID {cid} EN: {cards[cid]['en']}" for cid in report["cids"]["not_listed_ko_or_ja"]]
    catalog.write_atomic(path, ("\n".join(lines) + "\n").encode("utf-8"))


def main():
    parser = argparse.ArgumentParser(description="Official KO/JA rarity enrichment of official_cards.sqlite.")
    parser.add_argument("--snapshot", required=True, help="rarity snapshot id, e.g. the crawl date; reuse it to resume")
    args = parser.parse_args()
    if not re.fullmatch(r"[\w.-]+", args.snapshot) or not args.snapshot.strip("."):
        parser.error("--snapshot must be one directory name of letters, digits, '_', '-' and '.' (not '.' or '..')")
    report = enrich(args.snapshot)
    print(json.dumps({"status": report["status"], "errors": len(report["errors"]), "cids": report["cids"]}, ensure_ascii=False))
    if report["status"] != "complete":
        raise SystemExit(f"rarity enrichment is INCOMPLETE: {len(report['errors'])} errors; "
                         f"see {OFFICIAL_DIR / 'rarity_report.txt'}")


if __name__ == "__main__":
    main()
