"""Official OCG/TCG card inventory and DRAW2-missing official artwork export.

    .venv/Scripts/python -X utf8 inventory.py --snapshot 2026-10-07                   # crawl + map + images
    .venv/Scripts/python -X utf8 inventory.py --snapshot 2026-10-07 --inventory-only  # crawl + map only
    (an existing database enriched by rarities.py is refused unless --discard-rarity is given)

1. Crawl: every page of the official card database's full card list
   (card_search.action?ope=1&sess=1&rp=100&stype=1&keyword=) for request_locale
   ja, en and ko. Each validated page is checkpointed under
   data/official_cards/snapshots/<snapshot>/<locale>/page_NNN.json, so re-running
   the same --snapshot resumes an interrupted crawl. A page whose total differs
   from the snapshot's first page fails: the site changed, start a new snapshot.
2. Map: each DRAW2 output card_id -> the YGOPRODeck record holding it (id or any
   card_images id) -> that record's misc_info.konami_id, i.e. the official CID
   (or the unique exact official EN list name when the record has none). Those
   CIDs are supported. Other CIDs are unsupported only when every DRAW2 identity
   was placed; otherwise, or on a name conflict, they are "unresolved".
   Writes data/official_cards/official_cards.sqlite, manifest.json and reports.
3. Images: every artwork (ciid) the official detail pages list for each
   unsupported card, union over its listed locales (ja first), into
   data/references_missing/ in the references.py index schema. index.json is
   published only when no card failed.

Crawling approach from TrackerYGO src/crawling/scrape-cids.ts and scrape-cards.ts
(https://github.com/aziran07/TrackerYGO), without its swallowed errors, unsigned
image URLs or first-number total parsing.
"""

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

import catalog
import references

LOCALES = ("ja", "en", "ko")
PAGE_SIZE = 100  # rp above 100 (e.g. 99999) silently falls back to 10 rows
WORKERS = 3
OFFICIAL_DIR = Path("data/official_cards")
MISSING_DIR = Path("data/references_missing")
YGOPRODECK_PATH = Path("data/experiments/draw2_coverage/ygoprodeck_cardinfo_20261007.json")
YGOPRODECK_SHA256 = "45a64f963d95a1d3a4a3a68211e98cec44becd606f8a31f8277a72ad69da93aa"
MODEL_LABELS_PATH = Path("data/models/cardnames_onnx.json")
STATUSES = ("supported", "unsupported", "unresolved")
# DRAW2 identities verified to have no official CID: absent from all three full lists, and the official
# en name search answers div.no_data (checked 2026-10-07; control search "Dark Magician Girl" -> 4 rows).
OUTSIDE_OFFICIAL_DB = {111000561: "Get Your Game On! (2007 TCG World Championship prize card)"}

TOTAL_PATTERNS = {
    "ja": re.compile(r"検索結果\s*(?P<total>[\d,]+)件中\s*(?P<start>[\d,]+)[～~](?P<end>[\d,]+)件を表示"),
    "en": re.compile(r"Search Results:\s*(?P<start>[\d,]+)\s*-\s*(?P<end>[\d,]+)\s*of\s*(?P<total>[\d,]+)"),
    "ko": re.compile(r"검색결과\s*(?P<total>[\d,]+)건\s*중\s*(?P<start>[\d,]+)[～~](?P<end>[\d,]+)건을\s*표시"),
}
# List-row fields kept as text. Card text keeps its line breaks; the rest is whitespace-collapsed.
INFO_SELECTORS = {
    "ruby": ".card_ruby", "attribute": ".box_card_attribute", "effect": ".box_card_effect",
    "level_rank": ".box_card_level_rank", "link_markers": ".box_card_linkmarker",
    "species": ".card_info_species_and_other_item", "atk": ".atk_power", "def": ".def_power",
    "pendulum": "dd.box_card_pen_info", "text": "dd.box_card_text:not(.biko)", "note": "dd.box_card_text.biko",
}
MULTILINE_FIELDS = {"pendulum", "text", "note"}


class InventoryError(Exception):
    pass


def log(message):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, data):
    catalog.write_atomic(Path(path), json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))


def display_name(card):
    return card["name_ko"] or card["name_ja"] or card["name_en"]


# ---------------------------------------------------------------- list pages

def list_params(locale, page):
    return {"ope": 1, "sess": 1, "rp": PAGE_SIZE, "page": page, "stype": 1, "keyword": "", "request_locale": locale}


def element_text(element, multiline):
    text = element.get_text()
    if multiline:
        return "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())
    return " ".join(text.split())


def parse_row(row):
    cid_input = row.select_one("input.cid")
    link_input = row.select_one("input.link_value")
    link_match = re.search(r"[?&]cid=(\d+)", link_input.get("value", "")) if link_input else None
    if cid_input is None or not cid_input.get("value", "").isdigit() or link_match is None:
        raise InventoryError(f"row has no usable input.cid / input.link_value cid: {str(row)[:200]!r}")
    cid = int(cid_input["value"])
    if int(link_match.group(1)) != cid:
        raise InventoryError(f"row input.cid {cid} differs from link_value cid {link_match.group(1)}")
    # The visible name, not input.cnm: cnm's value attribute is broken by unescaped quotes
    # (CID 8610 "Aggiba, the Malevolent Sh'nn S'yo" reads "Aggiba, the Malevolent Sh").
    name_element = row.select_one(".card_name")
    name = element_text(name_element, False) if name_element else ""
    if not name:
        raise InventoryError(f"row for cid {cid} has no visible .card_name")
    info = {}
    for key, selector in INFO_SELECTORS.items():
        element = row.select_one(selector)
        if element is not None:
            text = element_text(element, key in MULTILINE_FIELDS)
            if text:
                info[key] = text
    return {"cid": cid, "name": name, "info": info}


def parse_list_page(html, locale):
    """{"total", "start", "end", "rows"} of one full-list page; malformed pages raise InventoryError."""
    soup = BeautifulSoup(html, "html.parser")
    total_element = soup.select_one(".sort_set .text")
    if total_element is None:
        raise InventoryError("list page has no .sort_set .text result count")
    total_text = element_text(total_element, False)
    match = TOTAL_PATTERNS[locale].fullmatch(total_text)
    if match is None:
        raise InventoryError(f"{locale} result count {total_text!r} does not match the verified format")
    numbers = {key: int(value.replace(",", "")) for key, value in match.groupdict().items()}
    rows = [parse_row(row) for row in soup.select(".t_row.c_normal.open")]
    return {**numbers, "rows": rows}


def validate_page(record, locale, page, expected_total):
    total = record["total"]
    if expected_total is not None and total != expected_total:
        raise InventoryError(f"{locale} page {page} reports {total} cards but the snapshot's page 1 reported "
                             f"{expected_total}; the site changed mid-snapshot, start a new --snapshot")
    pages = math.ceil(total / PAGE_SIZE)
    expected_start, expected_end = (page - 1) * PAGE_SIZE + 1, min(page * PAGE_SIZE, total)
    if total <= 0 or page > pages:
        raise InventoryError(f"{locale} page {page} is outside 1..{pages} for {total} cards")
    if (record["start"], record["end"]) != (expected_start, expected_end):
        raise InventoryError(f"{locale} page {page} shows {record['start']}-{record['end']}, "
                             f"expected {expected_start}-{expected_end}")
    if len(record["rows"]) != expected_end - expected_start + 1:
        raise InventoryError(f"{locale} page {page} has {len(record['rows'])} rows for range "
                             f"{expected_start}-{expected_end}")
    cids = [row["cid"] for row in record["rows"]]
    if len(set(cids)) != len(cids):
        raise InventoryError(f"{locale} page {page} lists a cid twice: {sorted(c for c, n in Counter(cids).items() if n > 1)}")


def load_or_fetch_page(session, directory, locale, page, expected_total):
    path = directory / f"page_{page:03}.json"
    if path.exists():
        record = json.loads(path.read_text(encoding="utf-8"))
        if (record.get("locale"), record.get("page")) != (locale, page):
            raise InventoryError(f"{path} is not the checkpoint of {locale} page {page}")
        validate_page(record, locale, page, expected_total)
        return record, True
    html = catalog.fetch_html(session, catalog.SEARCH_URL, params=list_params(locale, page))
    try:
        record = {"locale": locale, "page": page, "fetched_at": utc_now(),
                  "html_sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(), **parse_list_page(html, locale)}
        validate_page(record, locale, page, expected_total)
    except InventoryError as error:
        raise InventoryError(f"{catalog.SEARCH_URL} {list_params(locale, page)}: {error}") from error
    write_json(path, record)
    return record, False


def check_locale_pages(locale, records):
    """{cid: row} of a locale's pages; fails on cross-page duplicates or a count that misses the total."""
    total = records[0]["total"]
    rows, seen_on = {}, {}
    for record in records:
        for row in record["rows"]:
            if row["cid"] in rows:
                raise InventoryError(f"{locale}: cid {row['cid']} is listed on page {seen_on[row['cid']]} and "
                                     f"page {record['page']} (list order shifted during the crawl)")
            rows[row["cid"]], seen_on[row["cid"]] = row, record["page"]
    if len(rows) != total:
        raise InventoryError(f"{locale}: {len(rows)} unique cids but the site reports {total}")
    return rows


def crawl_locale(locale, snapshot_dir, session_factory=catalog.new_session):
    directory = snapshot_dir / locale
    directory.mkdir(parents=True, exist_ok=True)
    with session_factory() as session:  # one session per locale keeps the request_locale cookie consistent
        first, reused = load_or_fetch_page(session, directory, locale, 1, None)
        pages = math.ceil(first["total"] / PAGE_SIZE)
        records = [first]
        for page in range(2, pages + 1):
            record, was_reused = load_or_fetch_page(session, directory, locale, page, first["total"])
            records.append(record)
            reused += was_reused
            if page % 20 == 0 or page == pages:
                log(f"{locale}: page {page}/{pages}")
    rows = check_locale_pages(locale, records)
    fetched = [record["fetched_at"] for record in records]
    summary = {"reported_total": first["total"], "pages": pages, "unique_cids": len(rows),
               "reused_checkpoint_pages": int(reused), "first_fetched_at": min(fetched),
               "last_fetched_at": max(fetched)}
    return rows, summary


# ---------------------------------------------------------------- identity mapping

def load_mapping_sources():
    if sha256_file(YGOPRODECK_PATH) != YGOPRODECK_SHA256:
        raise InventoryError(f"{YGOPRODECK_PATH} does not have the audited sha256 {YGOPRODECK_SHA256}")
    ygo_cards = json.loads(YGOPRODECK_PATH.read_text(encoding="utf-8"))["data"]
    labels = json.loads(MODEL_LABELS_PATH.read_text(encoding="utf-8"))
    if len(ygo_cards) != 14597 or len(labels) != 13820 or len({e["card_id"] for e in labels.values()}) != 13819:
        raise InventoryError("mapping sources do not have the audited sizes (14597 cards, 13820 labels, 13819 ids)")
    return ygo_cards, labels


def ygoprodeck_records(ygo_cards):
    """Records with every passcode of the card (id + card_images ids) and its konami_id (official CID) or None."""
    records = []
    for card in ygo_cards:
        ids = [card["id"]] + [image["id"] for image in card["card_images"] if image["id"] != card["id"]]
        konami_id = card["misc_info"][0].get("konami_id") if card.get("misc_info") else None
        records.append({"id": card["id"], "ids": ids, "name": card["name"], "konami_id": konami_id})
    return records


def map_cards(official, ygo_cards, labels):
    """Map every DRAW2 output identity to an official CID, then classify each official CID.

    official: {cid: {"name_ja", "name_en", "name_ko", ...}}. Returns (mappings {cid: dict}, audit dict).
    A CID is "unsupported" only when every DRAW2 identity has been placed on some CID (or shown to be
    outside the official lists), so a gap in YGOPRODeck can never turn into a false "unsupported".
    """
    model_ids = {int(entry["card_id"]) for entry in labels.values()}
    label_names = defaultdict(set)
    for entry in labels.values():
        for lang in ("EN", "JA"):
            if entry.get(lang):
                label_names[(lang, catalog.normalize_name(entry[lang]))].add(int(entry["card_id"]))
    records = ygoprodeck_records(ygo_cards)
    by_konami, owners = defaultdict(list), defaultdict(list)
    for record in records:
        if record["konami_id"] is not None:
            by_konami[record["konami_id"]].append(record)
        for passcode in record["ids"]:
            owners[passcode].append(record)
    shared_konami = {k: [r["id"] for r in v] for k, v in by_konami.items() if len(v) > 1}
    if shared_konami:
        raise InventoryError(f"YGOPRODeck konami_ids on several records: {shared_konami}")
    ambiguous = {p: [r["id"] for r in owners[p]] for p in model_ids if len(owners.get(p, [])) > 1}
    if ambiguous:
        raise InventoryError(f"DRAW2 passcodes on several YGOPRODeck records: {ambiguous}")

    official_by_en = defaultdict(list)
    for cid, card in official.items():
        if card["name_en"]:
            official_by_en[catalog.normalize_name(card["name_en"])].append(cid)

    # DRAW2 identity -> official CID.
    draw2_by_cid, placement_method, unplaced, outside_db = defaultdict(set), {}, [], []
    for passcode in sorted(model_ids - set(owners)):
        unplaced.append({"passcode": passcode, "reason": "no YGOPRODeck record carries this passcode"})
    for record in records:
        hits = set(record["ids"]) & model_ids
        if not hits:
            continue
        cid, method = record["konami_id"], "ygoprodeck_konami_id"
        if cid is None:
            matches = official_by_en.get(catalog.normalize_name(record["name"]), [])
            if not matches and hits <= set(OUTSIDE_OFFICIAL_DB):
                outside_db.append({"passcodes": sorted(hits), "name_en": record["name"],
                                   "evidence": [OUTSIDE_OFFICIAL_DB[p] for p in sorted(hits)]})
                continue
            if len(matches) != 1:
                unplaced.append({"passcodes": sorted(hits), "name_en": record["name"],
                                 "reason": f"no konami_id and {len(matches)} official EN list names match exactly"})
                continue
            cid, method = matches[0], "ygoprodeck_record_exact_official_en_name"
        draw2_by_cid[cid] |= hits
        placement_method[cid] = method
    outside_lists = {cid: sorted(ids) for cid, ids in draw2_by_cid.items() if cid not in official}

    mappings = {}
    for cid, card in official.items():
        record = by_konami[cid][0] if cid in by_konami else None
        mapping = {"status": None, "method": None, "ygoprodeck_id": record["id"] if record else None,
                   "passcodes": record["ids"] if record else [], "draw2_card_ids": [], "note": None}
        if cid in draw2_by_cid:
            mapping.update(status="supported", method=placement_method[cid], draw2_card_ids=sorted(draw2_by_cid[cid]))
            if record is None:  # placed by name: the record is the one the name matched
                mapping["passcodes"] = sorted(draw2_by_cid[cid])
        elif unplaced:
            mapping.update(status="unresolved", note=f"{len(unplaced)} DRAW2 identities have no official CID yet")
        else:
            # An official name equal to a DRAW2 label name contradicts "unsupported" unless that label's identity
            # is placed on another CID carrying the same official name (two official cards share the name).
            unexplained, shared_with = set(), set()
            for lang, name in (("EN", card["name_en"]), ("JA", card["name_ja"])):
                if not name:
                    continue
                for passcode in label_names.get((lang, catalog.normalize_name(name)), set()):
                    owners_with_name = [c for c, ids in draw2_by_cid.items() if passcode in ids and c in official
                                        and official[c][f"name_{lang.lower()}"] == name]
                    if owners_with_name:
                        shared_with |= {(c, passcode) for c in owners_with_name}
                    else:
                        unexplained.add(passcode)
            if unexplained:
                mapping.update(status="unresolved", note=f"CID is not a DRAW2 identity but an official name equals "
                                                         f"DRAW2 label card_id {sorted(unexplained)}")
            else:
                mapping.update(status="unsupported",
                               method="ygoprodeck_konami_id" if record else "cid_not_in_draw2_identity_set")
                if shared_with:
                    mapping["note"] = "official name shared with " + ", ".join(
                        f"CID {c} (DRAW2 {p}, placed by konami_id)" for c, p in sorted(shared_with))
        mappings[cid] = mapping
    audit = {"draw2_identities": len(model_ids), "draw2_cids": len(draw2_by_cid),
             "placement_methods": dict(Counter(placement_method.values())),
             "unplaced_draw2_identities": unplaced,
             "draw2_identities_outside_official_db": outside_db,
             "draw2_cids_outside_official_lists": outside_lists}
    return mappings, audit


# ---------------------------------------------------------------- database and reports

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE locale_lists (locale TEXT PRIMARY KEY, reported_total INTEGER NOT NULL, pages INTEGER NOT NULL,
    unique_cids INTEGER NOT NULL, first_fetched_at TEXT NOT NULL, last_fetched_at TEXT NOT NULL);
CREATE TABLE cards (
    cid INTEGER PRIMARY KEY,
    name_ja TEXT, name_en TEXT, name_ko TEXT,   -- visible name in that locale's official list; NULL = not listed
    list_info TEXT NOT NULL,                    -- JSON {locale: list-row fields}
    draw2_status TEXT NOT NULL CHECK (draw2_status IN ('supported', 'unsupported', 'unresolved')),
    mapping_method TEXT, ygoprodeck_id INTEGER,
    passcodes TEXT NOT NULL,                    -- JSON list: YGOPRODeck id + card_images ids (or DRAW2 alias)
    draw2_card_ids TEXT NOT NULL,               -- JSON list: passcodes that are DRAW2 output labels
    mapping_note TEXT,
    CHECK ((draw2_status = 'supported') = (draw2_card_ids != '[]')));
"""


def union_cards(locale_rows):
    official = {}
    for locale in LOCALES:
        for cid, row in locale_rows[locale].items():
            card = official.setdefault(cid, {"cid": cid, "name_ja": None, "name_en": None, "name_ko": None, "info": {}})
            card[f"name_{locale}"] = row["name"]
            card["info"][locale] = row["info"]
    return dict(sorted(official.items()))


def write_database(path, official, mappings, summaries, meta):
    temp_path = path.with_name(path.name + ".staging")
    if temp_path.exists():
        temp_path.unlink()  # our own interrupted staging file, never published
    connection = sqlite3.connect(temp_path)
    try:
        connection.executescript(SCHEMA)
        connection.executemany("INSERT INTO meta VALUES (?, ?)", [(k, json.dumps(v, ensure_ascii=False)) for k, v in meta.items()])
        connection.executemany("INSERT INTO locale_lists VALUES (?, ?, ?, ?, ?, ?)",
                               [(locale, s["reported_total"], s["pages"], s["unique_cids"], s["first_fetched_at"],
                                 s["last_fetched_at"]) for locale, s in summaries.items()])
        connection.executemany(
            "INSERT INTO cards VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(cid, card["name_ja"], card["name_en"], card["name_ko"], json.dumps(card["info"], ensure_ascii=False),
              mappings[cid]["status"], mappings[cid]["method"], mappings[cid]["ygoprodeck_id"],
              json.dumps(mappings[cid]["passcodes"]), json.dumps(mappings[cid]["draw2_card_ids"]), mappings[cid]["note"])
             for cid, card in official.items()])
        connection.commit()
    finally:
        connection.close()
    os.replace(temp_path, path)


def write_card_report(path, title, header_lines, cards):
    lines = [title, *header_lines, "Display name: Korean > Japanese > English (official list names only).", ""]
    for number, card in enumerate(cards, 1):
        lines += [f"{number}. {display_name(card)}",
                  f"   CID {card['cid']} | KO: {card['name_ko'] or '(not listed)'} | JA: {card['name_ja'] or '(not listed)'}"
                  f" | EN: {card['name_en'] or '(not listed)'}"]
        if card.get("detail"):
            lines.append(f"   {card['detail']}")
    catalog.write_atomic(path, ("\n".join(lines) + "\n").encode("utf-8"))


def existing_rarity_tables(database_path):
    """Rarity tables (written by rarities.py) present in an existing inventory database."""
    if not database_path.exists():
        return []
    with closing(sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    return sorted(name for name in tables if name.startswith("rarity_"))


def build_inventory(snapshot, official_dir=OFFICIAL_DIR, session_factory=catalog.new_session, discard_rarity=False):
    """discard_rarity: the rebuilt database has no rarity tables; without it an enriched database is refused."""
    started = time.perf_counter()
    rarity_tables = existing_rarity_tables(official_dir / "official_cards.sqlite")
    if rarity_tables and not discard_rarity:
        raise InventoryError(f"{official_dir / 'official_cards.sqlite'} holds rarity tables {rarity_tables}; rebuilding "
                             "would drop them. Pass --discard-rarity and re-run rarities.py on the new inventory")
    snapshot_dir = official_dir / "snapshots" / snapshot
    log(f"crawling ja/en/ko official lists into {snapshot_dir} ({WORKERS} sessions)")
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {locale: pool.submit(crawl_locale, locale, snapshot_dir, session_factory) for locale in LOCALES}
        results = {locale: future.result() for locale, future in futures.items()}
    locale_rows = {locale: rows for locale, (rows, _) in results.items()}
    summaries = {locale: summary for locale, (_, summary) in results.items()}
    for locale, summary in summaries.items():
        log(f"{locale}: {summary['unique_cids']} cards / reported {summary['reported_total']} in {summary['pages']} pages")
    official = union_cards(locale_rows)

    mappings, mapping_audit = map_cards(official, *load_mapping_sources())
    status_counts = Counter(m["status"] for m in mappings.values())
    method_counts = Counter(m["method"] or "unresolved" for m in mappings.values())
    log(f"union {len(official)} CIDs: {dict(status_counts)}")

    meta = {"snapshot": snapshot, "built_at": utc_now(), "source": catalog.SEARCH_URL,
            "list_params": list_params("<locale>", "<page>"),
            "name_rule": "visible .card_name of each locale's list row",
            "mapping_rule": "each DRAW2 card_id -> the YGOPRODeck record holding it as id or card_images id -> "
                            "its misc_info.konami_id (else the unique exact official EN list name) = supported CID; "
                            "other CIDs are unsupported only if every DRAW2 identity was placed, else unresolved",
            "inputs_sha256": {str(p): sha256_file(p) for p in (YGOPRODECK_PATH, MODEL_LABELS_PATH)}}
    database_path = official_dir / "official_cards.sqlite"
    write_database(database_path, official, mappings, summaries, meta)

    cards = [{**official[cid], "mapping": mappings[cid]} for cid in official]
    header = [f"Snapshot {snapshot}; official lists ja {summaries['ja']['reported_total']}, "
              f"en {summaries['en']['reported_total']}, ko {summaries['ko']['reported_total']}; union {len(official)} CIDs."]
    def unsupported_detail(mapping):
        source = (f"YGOPRODeck {mapping['ygoprodeck_id']} has no DRAW2 passcode" if mapping["ygoprodeck_id"]
                  else "not in YGOPRODeck; CID is not among the DRAW2 identities")
        return source + (f"; {mapping['note']}" if mapping["note"] else "")

    unsupported = [{**c, "detail": unsupported_detail(c["mapping"])} for c in cards if c["mapping"]["status"] == "unsupported"]
    unresolved = [{**c, "detail": f"unresolved: {c['mapping']['note']}"} for c in cards if c["mapping"]["status"] == "unresolved"]
    write_card_report(official_dir / "unsupported_cards.txt",
                      f"Official cards with no DRAW2 output label ({len(unsupported)})", header, unsupported)
    write_card_report(official_dir / "unresolved_cards.txt",
                      f"Official cards whose DRAW2 support could not be determined ({len(unresolved)})", header, unresolved)

    manifest = {**meta, "status": "inventory_complete" if not status_counts["unresolved"] else "inventory_has_unresolved_cids", "locales": summaries, "union_cids": len(official),
                "draw2_status_counts": dict(status_counts), "mapping_method_counts": dict(method_counts), "draw2_identity_audit": mapping_audit,
                "database": {"path": database_path.as_posix(), "sha256": sha256_file(database_path),
                             "bytes": database_path.stat().st_size},
                "reports": [(official_dir / name).as_posix() for name in ("unsupported_cards.txt", "unresolved_cards.txt")],
                "inventory_seconds": round(time.perf_counter() - started, 1)}
    write_json(official_dir / "manifest.json", manifest)
    return cards, manifest


# ---------------------------------------------------------------- missing-card artwork export

def fetch_card_artworks(card, out_dir, session_factory):
    """Download every listed artwork of one card, union over its listed locales (ja, en, ko order)."""
    cid = card["cid"]
    artworks, placeholders = {}, []
    locales = [locale for locale in LOCALES if card[f"name_{locale}"] is not None]
    for locale in locales:
        url = catalog.detail_url(cid, locale)
        with session_factory() as session:  # images are signed per detail page; fetch them in the same session
            html = catalog.fetch_html(session, url)
            name, _ = catalog.parse_detail_page(html)
            if name is None:
                raise InventoryError(f"{url}: card is in the {locale} list but its detail page has no card")
            for ciid, src in sorted(references.artwork_sources(html, cid).items()):
                if ciid in artworks:
                    continue
                response = session.get(catalog.SITE + src, headers={"Referer": url}, timeout=catalog.TIMEOUT_SECONDS)
                response.raise_for_status()
                data = response.content
                sha256 = hashlib.sha256(data).hexdigest()
                if sha256 == catalog.PLACEHOLDER_IMAGE_SHA256:
                    placeholders.append({"ciid": ciid, "locale": locale})
                    continue
                extension = {"PNG": "png", "JPEG": "jpg"}[catalog.validate_image_bytes(data, catalog.SITE + src)]
                image = f"images/{cid}_{ciid}_{sha256[:12]}.{extension}"
                if not (out_dir / image).is_file():
                    catalog.write_atomic(out_dir / image, data)
                artworks[ciid] = {"ciid": ciid, "image": image, "sha256": sha256, "locale": locale}
    unavailable = sorted({p["ciid"] for p in placeholders} - set(artworks))
    return {"cid": cid, "locales": locales, "artworks": [artworks[c] for c in sorted(artworks)],
            "placeholder_attempts": placeholders, "unavailable_ciids": unavailable}


def checkpoint_key(card, snapshot):
    """What a checkpoint must still agree with to be reused."""
    return {"snapshot": snapshot, "names": [card[f"name_{locale}"] for locale in LOCALES],
            "status": card["mapping"]["status"], "method": card["mapping"]["method"],
            "passcodes": card["mapping"]["passcodes"]}


def load_checkpoint(path, card, snapshot, out_dir):
    checkpoint = json.loads(path.read_text(encoding="utf-8"))
    if checkpoint.get("key") != checkpoint_key(card, snapshot):
        raise InventoryError(f"{path} was made for a different snapshot or mapping of cid {card['cid']}; "
                             "move it aside to re-download")
    for artwork in checkpoint["result"]["artworks"]:
        image_path = out_dir / artwork["image"]
        if not image_path.is_file() or sha256_file(image_path) != artwork["sha256"]:
            raise InventoryError(f"{path}: {image_path} is missing or does not match its sha256")
    return checkpoint["result"]


def export_missing_artworks(cards, snapshot, out_dir=MISSING_DIR, session_factory=catalog.new_session):
    """Returns the export report; index.json is (re)published only when every card succeeded."""
    started = time.perf_counter()
    targets = [card for card in cards if card["mapping"]["status"] == "unsupported"]
    checkpoint_dir = out_dir / "checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    results, failures, reused = {}, [], 0

    def work(card):
        path = checkpoint_dir / f"{card['cid']}.json"
        if path.exists():
            return load_checkpoint(path, card, snapshot, out_dir), True
        result = fetch_card_artworks(card, out_dir, session_factory)
        write_json(path, {"key": checkpoint_key(card, snapshot), "completed_at": utc_now(), "result": result})
        return result, False

    log(f"exporting artworks of {len(targets)} unsupported cards into {out_dir} ({WORKERS} workers)")
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [(card, pool.submit(work, card)) for card in targets]
        for number, (card, future) in enumerate(futures, 1):
            try:
                results[card["cid"]], was_reused = future.result()
                reused += was_reused
            except (requests.RequestException, catalog.CatalogError, references.ReferenceLibraryError,
                    InventoryError, OSError, ValueError) as error:
                # Recorded and fails the export below; other cards still checkpoint so a rerun resumes.
                failures.append({"cid": card["cid"], "name": display_name(card), "error": f"{type(error).__name__}: {error}"})
            if number % 50 == 0 or number == len(futures):
                log(f"artworks: {number}/{len(futures)} cards done, {len(failures)} failed")

    entries, without_artwork = {}, []
    by_cid = {card["cid"]: card for card in targets}
    for cid, result in sorted(results.items()):
        card = by_cid[cid]
        if not result["artworks"]:
            without_artwork.append(cid)
            continue
        entries[str(cid)] = {"cid": cid, "name_en": card["name_en"], "name_ja": card["name_ja"],
                             "name_ko": card["name_ko"], "source_url": catalog.detail_url(cid, result["locales"][0]),
                             "artworks": [{k: a[k] for k in ("ciid", "image", "sha256")} for a in result["artworks"]]}

    complete = not failures and not without_artwork
    report = {
        "snapshot": snapshot, "finished_at": utc_now(),
        "status": "complete" if complete else "incomplete",
        "unsupported_cards": len(targets), "cards_exported": len(entries),
        "artworks_exported": sum(len(e["artworks"]) for e in entries.values()),
        "checkpoints_reused": reused,
        "failures": failures,
        "cards_without_artwork": [{"cid": cid, "name": display_name(by_cid[cid]),
                                   "placeholder_ciids": results[cid]["unavailable_ciids"]} for cid in without_artwork],
        "cards_with_unavailable_ciids": [{"cid": cid, "name": display_name(by_cid[cid]), "ciids": r["unavailable_ciids"]}
                                         for cid, r in sorted(results.items()) if r["unavailable_ciids"]],
        "seconds": round(time.perf_counter() - started, 1),
    }
    if complete:
        index = {"schema": references.SCHEMA_VERSION, "cards": entries}
        for key, entry in entries.items():
            references._validate_card(key, entry, out_dir)
        write_json(out_dir / references.INDEX_NAME, index)
        if references.load_index(out_dir) != index:
            raise InventoryError(f"{out_dir / references.INDEX_NAME} does not read back as written")
        report["index"] = {"path": (out_dir / references.INDEX_NAME).as_posix(),
                           "sha256": sha256_file(out_dir / references.INDEX_NAME)}
    write_json(out_dir / "export_report.json", report)
    write_export_text(out_dir / "export_report.txt", report, by_cid, results)
    return report


def write_export_text(path, report, by_cid, results):
    lines = [f"DRAW2-missing official artwork export: {report['status'].upper()}",
             f"Snapshot {report['snapshot']}, finished {report['finished_at']}",
             f"Unsupported cards {report['unsupported_cards']}, exported {report['cards_exported']} cards / "
             f"{report['artworks_exported']} artworks, failures {len(report['failures'])}, "
             f"cards without any artwork {len(report['cards_without_artwork'])}",
             "Display name: Korean > Japanese > English (official list names only).", ""]
    if report["failures"]:
        lines += ["== Failures (rerun the same command to resume) =="]
        lines += [f"- CID {f['cid']} {f['name']}: {f['error']}" for f in report["failures"]] + [""]
    if report["cards_without_artwork"]:
        lines += ["== Cards with only 'Coming Soon' placeholders (not exported) =="]
        lines += [f"- CID {c['cid']} {c['name']}: ciids {c['placeholder_ciids']}" for c in report["cards_without_artwork"]] + [""]
    if report["cards_with_unavailable_ciids"]:
        lines += ["== Exported cards with some placeholder-only ciids =="]
        lines += [f"- CID {c['cid']} {c['name']}: ciids {c['ciids']}" for c in report["cards_with_unavailable_ciids"]] + [""]
    lines += ["== Exported cards =="]
    for cid, result in sorted(results.items()):
        if result["artworks"]:
            card = by_cid[cid]
            lines.append(f"- CID {cid} {display_name(card)}: ciids {[a['ciid'] for a in result['artworks']]}")
    catalog.write_atomic(path, ("\n".join(lines) + "\n").encode("utf-8"))


def main():
    parser = argparse.ArgumentParser(description="Official card inventory and DRAW2-missing artwork export.")
    parser.add_argument("--snapshot", required=True, help="snapshot id, e.g. the crawl date; reuse it to resume")
    parser.add_argument("--inventory-only", action="store_true", help="stop after the inventory database")
    parser.add_argument("--discard-rarity", action="store_true",
                        help="allow rebuilding a database that rarities.py enriched (its rarity tables are dropped)")
    args = parser.parse_args()
    if not re.fullmatch(r"[\w.-]+", args.snapshot) or not args.snapshot.strip("."):
        parser.error("--snapshot must be one directory name of letters, digits, '_', '-' and '.' (not '.' or '..')")

    cards, manifest = build_inventory(args.snapshot, discard_rarity=args.discard_rarity)
    print(json.dumps({k: manifest[k] for k in ("locales", "union_cids", "draw2_status_counts", "database")},
                     ensure_ascii=False, indent=1))
    unresolved = [card["cid"] for card in cards if card["mapping"]["status"] == "unresolved"]
    if args.inventory_only:
        if unresolved:
            raise SystemExit(f"identification is INCOMPLETE: {len(unresolved)} unresolved CIDs; "
                             f"see {OFFICIAL_DIR / 'unresolved_cards.txt'}")
        return
    report = export_missing_artworks(cards, args.snapshot)
    manifest["artwork_export"] = {k: v for k, v in report.items()
                                  if k not in ("failures", "cards_without_artwork", "cards_with_unavailable_ciids")}
    manifest["artwork_export"].update(failures=len(report["failures"]), cards_without_artwork=len(report["cards_without_artwork"]),
                                      report=(MISSING_DIR / "export_report.json").as_posix())
    write_json(OFFICIAL_DIR / "manifest.json", manifest)
    print(json.dumps(manifest["artwork_export"], ensure_ascii=False, indent=1))
    if unresolved:
        raise SystemExit(f"identification is INCOMPLETE: {len(unresolved)} unresolved CIDs were not exported; "
                         f"see {OFFICIAL_DIR / 'unresolved_cards.txt'}")
    if report["status"] != "complete":
        raise SystemExit(f"artwork export is INCOMPLETE: {len(report['failures'])} failures, "
                         f"{len(report['cards_without_artwork'])} cards without artwork; see {MISSING_DIR / 'export_report.txt'}")


if __name__ == "__main__":
    main()
