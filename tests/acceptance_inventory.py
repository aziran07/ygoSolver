"""Codex-authored acceptance checks, independent of the crawler implementation.

Run after a completed collection:
    .venv/Scripts/python -X utf8 tests/acceptance_inventory.py
Add --live to compare sampled lists and artwork bytes with fresh Konami responses.
This intentionally imports no inventory/catalog/references implementation code.
Missing artifacts, unresolved identities and incomplete exports are failures.
"""

import hashlib
import io
import json
import re
import sqlite3
import sys
import unittest
from collections import defaultdict
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
OFFICIAL = ROOT / "data/official_cards"
LIBRARY = ROOT / "data/references_missing"
AUDIT = ROOT / "data/experiments/draw2_coverage"
SITE = "https://www.db.yugioh-card.com"
SEARCH = SITE + "/yugiohdb/card_search.action"
PLACEHOLDER_SHA256 = "d12496b32a693cf194b734860e294bd3418b6f4266becf854041919a5fb20984"
KNOWN_MISSING = {22722, 22723, 22691, 22704, 22715}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


class InventoryAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = read_json(OFFICIAL / "manifest.json")
        cls.report = read_json(LIBRARY / "export_report.json")
        cls.index = read_json(LIBRARY / "index.json")
        database = OFFICIAL / "official_cards.sqlite"
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise AssertionError("SQLite integrity check failed")
            cls.cards = {r["cid"]: dict(r) for r in connection.execute("SELECT * FROM cards")}
            cls.locales = {r["locale"]: dict(r) for r in connection.execute("SELECT * FROM locale_lists")}
        cls.api_cards = read_json(AUDIT / "ygoprodeck_cardinfo_20261007.json")["data"]
        cls.model_ids = {
            int(label["card_id"])
            for label in read_json(ROOT / "data/models/cardnames_onnx.json").values()
        }

    def test_every_snapshot_row_is_preserved_once_in_database(self):
        self.assertEqual(set(self.locales), {"ja", "en", "ko"})
        union = set()
        for locale, summary in self.locales.items():
            with self.subTest(locale=locale):
                total = summary["reported_total"]
                self.assertGreater(total, 13000)
                page_count = (total + 99) // 100
                directory = OFFICIAL / "snapshots" / self.manifest["snapshot"] / locale
                files = sorted(directory.glob("page_*.json"))
                self.assertEqual(len(files), page_count)
                rows_by_cid = {}
                for page_number, path in enumerate(files, 1):
                    page = read_json(path)
                    self.assertEqual((page["locale"], page["page"], page["total"]),
                                     (locale, page_number, total))
                    start = (page_number - 1) * 100 + 1
                    end = min(page_number * 100, total)
                    self.assertEqual((page["start"], page["end"]), (start, end))
                    self.assertEqual(len(page["rows"]), end - start + 1)
                    for row in page["rows"]:
                        cid = row["cid"]
                        self.assertNotIn(cid, rows_by_cid, f"duplicate CID {cid} in {path}")
                        self.assertIs(type(cid), int)
                        self.assertGreater(cid, 0)
                        self.assertTrue(row["name"].strip())
                        rows_by_cid[cid] = row
                        self.assertEqual(self.cards[cid][f"name_{locale}"], row["name"])
                        self.assertEqual(json.loads(self.cards[cid]["list_info"])[locale], row["info"])
                stored = {cid for cid, card in self.cards.items() if card[f"name_{locale}"] is not None}
                self.assertEqual(stored, set(rows_by_cid))
                self.assertEqual(len(stored), total)
                self.assertEqual(summary["unique_cids"], total)
                union.update(stored)
        self.assertEqual(set(self.cards), union)
        self.assertEqual(self.manifest["union_cids"], len(union))
        self.assertEqual(self.cards[8610]["name_ja"], "Aggiba, the Malevolent Sh'nn S'yo")

    def test_support_against_all_original_classifier_ids(self):
        # Build the inverse relationship directly from the independent API snapshot.
        owners = defaultdict(list)
        for card in self.api_cards:
            for identity in {card["id"]} | {image["id"] for image in card["card_images"]}:
                owners[identity].append(card)
        supported = defaultdict(set)
        for model_id in self.model_ids:
            self.assertEqual(len(owners[model_id]), 1, f"ambiguous/unmapped model ID {model_id}")
            source = owners[model_id][0]
            cid = source["misc_info"][0].get("konami_id")
            if cid is None:
                # Independently investigated exception: this promotional card has no official listing.
                self.assertEqual((model_id, source["name"]), (111000561, "Get Your Game On!"))
                self.assertFalse(any(c["name_en"] == source["name"] for c in self.cards.values()))
            elif cid in self.cards:
                supported[cid].add(model_id)
        for cid, card in self.cards.items():
            with self.subTest(cid=cid):
                expected_ids = supported.get(cid, set())
                self.assertEqual(set(json.loads(card["draw2_card_ids"])), expected_ids)
                self.assertEqual(card["draw2_status"], "supported" if expected_ids else "unsupported")
        self.assertEqual(self.cards[22580]["name_ko"], "패자의 명동")
        self.assertEqual(self.cards[22580]["draw2_status"], "supported")
        for cid in KNOWN_MISSING:
            self.assertEqual(self.cards[cid]["draw2_status"], "unsupported")
        dark_magician = next(c for c in self.cards.values() if c["name_en"] == "Dark Magician")
        self.assertIn(46986414, json.loads(dark_magician["draw2_card_ids"]))
        self.assertEqual(self.cards[4370]["draw2_status"], "supported")
        self.assertEqual(self.cards[19092]["draw2_status"], "unsupported")

    def test_published_provenance_matches_actual_files(self):
        database = OFFICIAL / "official_cards.sqlite"
        self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), self.manifest["database"]["sha256"])
        self.assertEqual(database.stat().st_size, self.manifest["database"]["bytes"])
        self.assertEqual(self.manifest["snapshot"], self.report["snapshot"])
        for relative, digest in self.manifest["inputs_sha256"].items():
            self.assertEqual(hashlib.sha256((ROOT / relative).read_bytes()).hexdigest(), digest)
        self.assertEqual(hashlib.sha256((LIBRARY / "index.json").read_bytes()).hexdigest(),
                         self.report["index"]["sha256"])

    def test_missing_library_has_exact_coverage_and_valid_images(self):
        expected = {cid for cid, c in self.cards.items() if c["draw2_status"] == "unsupported"}
        entries = self.index["cards"]
        self.assertEqual(self.index["schema"], 1)
        self.assertEqual({int(key) for key in entries}, expected)
        self.assertEqual(self.report["status"], "complete")
        self.assertEqual(self.report["failures"], [])
        self.assertEqual(self.report["cards_without_artwork"], [])
        self.assertEqual(self.report["cards_exported"], len(expected))
        files = set()
        count = 0
        for key, entry in entries.items():
            with self.subTest(cid=key):
                cid = int(key)
                self.assertEqual(entry["cid"], cid)
                for locale in self.locales:
                    self.assertEqual(entry[f"name_{locale}"], self.cards[cid][f"name_{locale}"])
                source = urlparse(entry["source_url"])
                self.assertEqual(source.netloc, "www.db.yugioh-card.com")
                self.assertEqual(parse_qs(source.query)["cid"], [key])
                self.assertTrue(entry["artworks"])
                ciids = set()
                for art in entry["artworks"]:
                    self.assertNotIn(art["ciid"], ciids)
                    ciids.add(art["ciid"])
                    self.assertRegex(art["image"], rf"^images/{cid}_{art['ciid']}_[0-9a-f]{{12}}\.(jpg|png)$")
                    path = (LIBRARY / art["image"]).resolve()
                    self.assertTrue(path.is_relative_to(LIBRARY.resolve()))
                    data = path.read_bytes()
                    self.assertEqual(hashlib.sha256(data).hexdigest(), art["sha256"])
                    self.assertNotEqual(art["sha256"], PLACEHOLDER_SHA256)
                    with Image.open(io.BytesIO(data)) as image:
                        image.load()
                        self.assertIn(image.format, {"PNG", "JPEG"})
                        self.assertGreater(image.width, 100)
                        self.assertGreater(image.height, image.width)
                    self.assertNotIn(path, files)
                    files.add(path)
                    count += 1
        self.assertEqual(self.report["artworks_exported"], count)
        self.assertEqual(files, {p.resolve() for p in (LIBRARY / "images").iterdir() if p.is_file()})

    def compare_fresh_official_sources(self):
        # First, middle, and last pages exercise names, pagination and partial final pages.
        for locale, summary in self.locales.items():
            with requests.Session() as session:
                for page in (1, (summary["pages"] + 1) // 2, summary["pages"]):
                    response = session.get(SEARCH, params={"ope": 1, "rp": 100, "page": page,
                                                          "request_locale": locale}, timeout=30)
                    response.raise_for_status()
                    soup = BeautifulSoup(response.text, "html.parser")
                    text = soup.select_one(".sort_set .text").get_text(" ", strip=True)
                    numbers = [int(n.replace(",", "")) for n in re.findall(r"[0-9][0-9,]*", text)]
                    total, first, last = (numbers[2], numbers[0], numbers[1]) if locale == "en" else numbers
                    self.assertEqual((total, first, last), (summary["reported_total"], (page - 1) * 100 + 1,
                                                           min(page * 100, summary["reported_total"])))
                    fresh = {int(r.select_one("input.cid")["value"]):
                             " ".join(r.select_one(".card_name").get_text().split())
                             for r in soup.select(".t_row.c_normal.open")}
                    cached = read_json(OFFICIAL / "snapshots" / self.manifest["snapshot"] / locale / f"page_{page:03d}.json")
                    self.assertEqual(fresh, {r["cid"]: r["name"] for r in cached["rows"]})
        # Known userDataset cards: independently request every artwork and compare the actual bytes.
        for cid in sorted(KNOWN_MISSING):
            entry = self.index["cards"][str(cid)]
            with requests.Session() as session:
                response = session.get(entry["source_url"], timeout=30)
                response.raise_for_status()
                advertised = {}
                for selector, relative in re.findall(r"\$\('#card_image_(\d+)'\)\.attr\('src',\s*'([^']+)'\)", response.text):
                    query = parse_qs(urlparse(relative).query)
                    self.assertEqual(query["cid"], [str(cid)])
                    self.assertEqual(query["ciid"], [selector])
                    advertised[int(selector)] = relative
                self.assertTrue(advertised)
                saved = {a["ciid"]: a for a in entry["artworks"]}
                for ciid, relative in advertised.items():
                    image = session.get(SITE + relative, headers={"Referer": entry["source_url"]}, timeout=30)
                    image.raise_for_status()
                    self.assertEqual(hashlib.sha256(image.content).hexdigest(), saved[ciid]["sha256"])


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(InventoryAcceptance)
    if "--live" in sys.argv[1:]:
        suite.addTest(InventoryAcceptance("compare_fresh_official_sources"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
