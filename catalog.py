"""Resolve an exact English card name to official Konami card data.

Source: the official Yu-Gi-Oh! card database (https://www.db.yugioh-card.com/yugiohdb/).
Crawling approach inspired by TrackerYGO src/crawling/scrape-cards.ts and scrape-cids.ts
(https://github.com/aziran07/TrackerYGO), without its swallowed errors.

Failure semantics:
- requests exceptions (network, HTTP status) propagate unchanged.
- CatalogError means the name is unknown/ambiguous, the HTML was not what we verified,
  or an image/cache file failed validation.
- None for name_ko/name_ja means Konami's detail page for that locale answered with its
  verified "no card information" block (div.no_data), i.e. the card is not released there.
"""

import argparse
import hashlib
import io
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path

import requests
from bs4 import BeautifulSoup, NavigableString
from PIL import Image

SITE = "https://www.db.yugioh-card.com"
SEARCH_URL = SITE + "/yugiohdb/card_search.action"
RESULTS_PER_PAGE = 100
MAX_NAME_LENGTH = 200
CARD_FIELDS = {"cid", "name_en", "name_ko", "name_ja", "image_path", "source_url"}
TIMEOUT_SECONDS = 30
USER_AGENT = "Mozilla/5.0 (ygoSolver catalog)"
# get_image.action answers a wrong/missing enc with HTTP 200 and this "Coming Soon" PNG
# (verified identical for en/ko/ja sessions and image types 1 and 2).
PLACEHOLDER_IMAGE_SHA256 = "d12496b32a693cf194b734860e294bd3418b6f4266becf854041919a5fb20984"
DETAIL_IMAGE_PATTERN = re.compile(r"\$\('#card_image_1'\)\.attr\('src',\s*'([^']+)'\)")
RESULT_TOTAL_PATTERN = re.compile(r"of\s+([\d,]+)")


class CatalogError(Exception):
    pass


# The card recognition model labels these cards without the katakana middle dot (U+30FB) that
# Konami's official English names contain (verified live English searches, saved in
# data/experiments/reactor_name/: Spell Reactor ・RE cid 8002, Trap Reactor ・Y FI cid 8000,
# Summon Reactor ・SK cid 7998). Only these verified names are mapped, onto the undotted keys
# already used by existing lookup caches; every other name still has to match exactly.
# The model labels passcode 70383419 "Invoked Babalon"; its official name is Invoked Baybarron.
OFFICIAL_NAME_KEY_ALIASES = {
    "spell reactor ・re": "spell reactor re",
    "trap reactor ・y fi": "trap reactor y fi",
    "summon reactor ・sk": "summon reactor sk",
    "invoked baybarron": "invoked babalon",
    # The model labels passcode 30271097 with an HTML-escaped ampersand.
    "the fallen & the virtuous": "the fallen &amp; the virtuous",
}
# Konami's search finds nothing for some model names, so these keys are searched by their
# official English name instead (verified live in data/experiments/babalon_name/: "Invoked
# Babalon" returns 0 results, "Invoked Baybarron" returns exactly cid 22980, passcode 70383419;
# "The Fallen &amp; The Virtuous" returns 0, "The Fallen & The Virtuous" exactly cid 22089).
OFFICIAL_SEARCH_KEYWORDS = {
    "invoked babalon": "Invoked Baybarron",
    "the fallen &amp; the virtuous": "The Fallen & The Virtuous",
}


def normalize_name(name):
    key = " ".join(unicodedata.normalize("NFKC", name).split()).casefold()
    return OFFICIAL_NAME_KEY_ALIASES.get(key, key)


def detail_url(cid, locale):
    return f"{SEARCH_URL}?ope=2&cid={cid}&request_locale={locale}"


def parse_search_page(html):
    """Return (total_results, [(visible name_en, cid), ...]) from an English search result page."""
    soup = BeautifulSoup(html, "html.parser")
    result_rows = soup.select(".t_row.c_normal.open")
    if not result_rows:
        if soup.select_one("div.no_data") is None:
            raise CatalogError("search page has neither result rows nor div.no_data")
        return 0, []

    rows = []
    for number, row in enumerate(result_rows, 1):
        # The visible name, not input.cnm: Konami leaves quotes in cnm's value unescaped
        # (e.g. "Evil★Twin's Trouble Sunny" parses as "Evil★Twin").
        names = row.select(".card_name")
        links = row.select("input.link_value")
        name = names[0].get_text().strip() if len(names) == 1 else ""
        if not name:
            raise CatalogError(f"search row {number} needs exactly one non-blank .card_name, found {len(names)}")
        if len(links) != 1:
            raise CatalogError(f"search row {number} ({name!r}) needs exactly one input.link_value, found {len(links)}")
        cid_match = re.search(r"[?&]cid=(\d+)", links[0].get("value", ""))
        if cid_match is None:
            raise CatalogError(f"search row {number} ({name!r}) link has no cid: {links[0].get('value')!r}")
        rows.append((name, int(cid_match.group(1))))

    sort_set = soup.select_one(".sort_set")
    total_match = RESULT_TOTAL_PATTERN.search(sort_set.get_text()) if sort_set else None
    if total_match is None:
        raise CatalogError("search page has rows but no 'Search Results: x - y of N' total")
    return int(total_match.group(1).replace(",", "")), rows


def parse_detail_page(html):
    """Return (localized_name, image_src) or (None, None) when Konami has no card for the locale."""
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one("#cardname h1")
    if heading is None:
        if soup.select_one("div.no_data") is None:
            raise CatalogError("detail page has neither #cardname nor div.no_data")
        return None, None
    # Direct text only: <span class="ruby"> holds the Japanese reading, the bare <span> the English name.
    name = " ".join("".join(c for c in heading.children if isinstance(c, NavigableString)).split())
    if not name:
        raise CatalogError("detail page #cardname h1 has no localized name text")
    image_match = DETAIL_IMAGE_PATTERN.search(html)
    if image_match is None:
        raise CatalogError("detail page has no $('#card_image_1').attr('src', ...) script")
    image_src = image_match.group(1)
    if "enc=" not in image_src:
        raise CatalogError(f"detail image src has no enc parameter: {image_src!r}")
    return name, image_src


def fetch_html(session, url, referer=None, params=None):
    headers = {"Referer": referer} if referer else {}
    response = session.get(url, params=params, headers=headers, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    if "text/html" not in response.headers.get("Content-Type", ""):
        raise CatalogError(f"{response.url} returned {response.headers.get('Content-Type')!r}, expected HTML")
    return response.text


def fetch_detail(session, url):
    html = fetch_html(session, url)
    try:
        return parse_detail_page(html)
    except CatalogError as error:
        raise CatalogError(f"{url}: {error}") from error


def find_cid(session, name_en):
    wanted = normalize_name(name_en)
    keyword = OFFICIAL_SEARCH_KEYWORDS.get(wanted, name_en)
    matching_cids = set()
    page = 1
    while True:
        params = {"ope": 1, "sess": 1, "rp": RESULTS_PER_PAGE, "page": page,
                  "stype": 1, "keyword": keyword, "request_locale": "en"}
        html = fetch_html(session, SEARCH_URL, params=params)
        try:
            total, rows = parse_search_page(html)
        except CatalogError as error:
            raise CatalogError(f"{SEARCH_URL} search page {page} for {name_en!r}: {error}") from error
        matching_cids.update(cid for name, cid in rows if normalize_name(name) == wanted)
        if page * RESULTS_PER_PAGE >= total:
            break
        page += 1

    if not matching_cids:
        raise CatalogError(f"no official card named exactly {name_en!r} (search returned {total} results)")
    if len(matching_cids) > 1:
        raise CatalogError(f"name {name_en!r} matches several cids: {sorted(matching_cids)}")
    return matching_cids.pop()


def download_image(session, image_src, referer, destination_stem):
    """Fetch image_src in the current session (its locale decides the card text) and save it."""
    response = session.get(SITE + image_src, headers={"Referer": referer}, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    data = response.content
    # Content-Type is application/octet-stream even for valid images, so validate the bytes instead.
    extension = {"PNG": ".png", "JPEG": ".jpg"}[validate_image_bytes(data, image_src)]
    path = destination_stem.with_suffix(extension)
    write_atomic(path, data)
    return path


def validate_image_bytes(data, source):
    """Return the format of a fully decoded PNG/JPEG card image, or raise CatalogError naming `source`."""
    if hashlib.sha256(data).hexdigest() == PLACEHOLDER_IMAGE_SHA256:
        raise CatalogError(f"{source} is Konami's 'Coming Soon' placeholder, not a card image")
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            image_format = image.format
    except Exception as error:
        raise CatalogError(f"{source} is not a decodable image: {error}") from error
    if image_format not in ("PNG", "JPEG"):
        raise CatalogError(f"{source} has unexpected image format {image_format!r}")
    return image_format


def write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(file_descriptor, "wb") as temp_file:
            temp_file.write(data)
        os.replace(temp_name, path)
    except BaseException:
        os.unlink(temp_name)
        raise


def new_session():
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    return session


def load_cache(cache_path):
    if not cache_path.exists():
        return {}
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise CatalogError(f"catalog cache {cache_path} is not valid JSON: {error}") from error
    if not isinstance(cache, dict):
        raise CatalogError(f"catalog cache {cache_path} is not a JSON object")
    return cache


def validated_cached_card(cache_path, key, record, data_dir):
    """Return a cached record with an absolute image_path, or raise CatalogError on any inconsistency."""
    problem = None
    if not isinstance(record, dict) or set(record) != CARD_FIELDS:
        problem = f"has fields {sorted(record) if isinstance(record, dict) else type(record).__name__}"
    elif type(record["cid"]) is not int or record["cid"] <= 0:
        problem = f"has invalid cid {record['cid']!r}"
    elif not isinstance(record["name_en"], str) or normalize_name(record["name_en"]) != key:
        problem = f"has name_en {record['name_en']!r} that does not match its key"
    elif any(record[field] is not None and not isinstance(record[field], str) for field in ("name_ko", "name_ja")):
        problem = "has non-string localized names"
    elif record["source_url"] != detail_url(record["cid"], "en"):
        problem = f"has unexpected source_url {record['source_url']!r}"
    elif not isinstance(record["image_path"], str):
        problem = f"has invalid image_path {record['image_path']!r}"
    if problem is not None:
        raise CatalogError(f"catalog cache {cache_path} entry {key!r} {problem}")

    image_path = data_dir / record["image_path"]
    if not image_path.is_file():
        raise CatalogError(f"catalog cache {cache_path} entry {key!r} image is missing: {image_path}")
    validate_image_bytes(image_path.read_bytes(), image_path)
    return {**record, "image_path": str(image_path)}


def resolve_card(name_en: str, data_dir=Path("data/catalog")) -> dict:
    if not isinstance(name_en, str):
        raise CatalogError(f"card name must be a string, got {type(name_en).__name__}")
    key = normalize_name(name_en)
    if not key:
        raise CatalogError("card name is blank")
    if len(key) > MAX_NAME_LENGTH:
        raise CatalogError(f"card name is longer than {MAX_NAME_LENGTH} characters")

    data_dir = Path(data_dir)
    cache_path = data_dir / "cards.json"
    # ponytail: whole-file JSON cache without locking; one writer at a time assumed.
    cache = load_cache(cache_path)
    if key in cache:
        return validated_cached_card(cache_path, key, cache[key], data_dir)

    with new_session() as session:
        card = fetch_card(session, name_en, key, data_dir)
    cache[key] = card
    write_atomic(cache_path, json.dumps(cache, ensure_ascii=False, indent=2).encode("utf-8"))
    return {**card, "image_path": str(data_dir / card["image_path"])}


def fetch_card(session, name_en, key, data_dir):
    cid = find_cid(session, name_en)

    # The image is fetched right after the English detail page so the session renders English card text.
    source_url = detail_url(cid, "en")
    official_name_en, image_src = fetch_detail(session, source_url)
    if official_name_en is None:
        raise CatalogError(f"search listed cid {cid} but its English detail page has no card")
    if normalize_name(official_name_en) != key:
        raise CatalogError(f"cid {cid} detail name {official_name_en!r} differs from {name_en!r}")
    image_path = download_image(session, image_src, source_url, data_dir / "images" / f"{cid}_en")

    name_ko, _ = fetch_detail(session, detail_url(cid, "ko"))
    name_ja, _ = fetch_detail(session, detail_url(cid, "ja"))

    return {
        "cid": cid,
        "name_en": official_name_en,
        "name_ko": name_ko,
        "name_ja": name_ja,
        "image_path": image_path.relative_to(data_dir).as_posix(),
        "source_url": source_url,
    }


def save_locale_sample(cid, locale, data_dir=Path("data/catalog")):
    """Save Konami's reference image rendered for `locale`; None if the card is not released there."""
    page_url = detail_url(cid, locale)
    with new_session() as session:
        name, image_src = fetch_detail(session, page_url)
        if name is None:
            return None
        return download_image(session, image_src, page_url, Path(data_dir) / "samples" / f"{cid}_{locale}")


def main():
    parser = argparse.ArgumentParser(description="Resolve an exact English card name via the official Konami DB.")
    parser.add_argument("--name", required=True, help="exact English card name")
    parser.add_argument("--data-dir", type=Path, default=Path("data/catalog"))
    parser.add_argument("--samples", action="store_true", help="also save ko/ja reference images to <data-dir>/samples")
    args = parser.parse_args()

    card = resolve_card(args.name, args.data_dir)
    if args.samples:
        for locale in ("ko", "ja"):
            path = save_locale_sample(card["cid"], locale, args.data_dir)
            card[f"sample_{locale}"] = None if path is None else str(path)
    print(json.dumps(card, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
