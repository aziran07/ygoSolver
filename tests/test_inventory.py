"""Offline tests for inventory.py: synthetic official list/detail pages and a fake HTTP session."""

import io
import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import requests
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import catalog  # noqa: E402
import inventory  # noqa: E402
import references  # noqa: E402
from inventory import InventoryError  # noqa: E402

TOTAL_TEXT = {
    "ja": "検索結果 {total:,}件中 {start:,}～{end:,}件を表示",
    "en": "Search Results: {start:,} - {end:,} of {total:,}",
    "ko": "검색결과 {total:,}건 중 {start:,}～{end:,}건을 표시",
}


def list_row(cid, name, cnm=None):
    cnm_input = cnm if cnm is not None else f'<input class="cnm" type="hidden" value="{name}"/>'
    return (f'<div class="t_row c_normal open"><dl><dd class="box_card_name"><span class="card_name">\n\t{name}\n\t</span>'
            f'</dd><dd class="remove_btn"><input class="cid" type="hidden" value="{cid}"/></dd>'
            f'<dd class="box_card_spec"><span class="box_card_attribute"><span>SPELL</span></span></dd>'
            f'<dd class="box_card_text c_text">Line one.\n\nLine two.</dd></dl>{cnm_input}'
            f'<input class="link_value" type="hidden" value="/yugiohdb/card_search.action?ope=2&amp;cid={cid}"/></div>')


def list_page(locale, total, start, rows):
    end = start + len(rows) - 1
    text = TOTAL_TEXT[locale].format(total=total, start=start, end=end)
    return f'<html><div class="sort_set"><div class="text">\n {text}\n</div></div>{"".join(rows)}</html>'


class FakeResponse:
    def __init__(self, url, body):
        self.url, self.status_code = url, 200
        self.headers = {"Content-Type": "application/octet-stream" if isinstance(body, bytes) else "text/html"}
        self.content = body if isinstance(body, bytes) else body.encode("utf-8")
        self.text = body if isinstance(body, str) else ""

    def raise_for_status(self):
        pass


class FakeSession:
    """Serves list pages by (locale, page) and other URLs by exact URL."""

    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, params=None, headers=None, timeout=None):
        key = (params["request_locale"], params["page"]) if params else url
        body = self.pages[key]
        if isinstance(body, Exception):
            raise body
        return FakeResponse(url, body)


def png(color):
    buffer = io.BytesIO()
    Image.new("RGB", (40, 60), color).save(buffer, "PNG")
    return buffer.getvalue()


class ListPageTests(unittest.TestCase):
    def test_reads_each_locale_total_format(self):
        for locale in inventory.LOCALES:
            page = inventory.parse_list_page(list_page(locale, 14305, 101, [list_row(7, "A")]), locale)
            self.assertEqual((page["total"], page["start"], page["end"]), (14305, 101, 101), locale)

    def test_wrong_locale_format_fails(self):
        with self.assertRaisesRegex(InventoryError, "verified format"):
            inventory.parse_list_page(list_page("en", 5, 1, [list_row(7, "A")]), "ja")

    def test_visible_name_wins_over_broken_cnm_attribute(self):
        broken = '<input class="cnm" nn="" s\'yo\'="" type="hidden" value="Aggiba, the Malevolent Sh"/>'
        page = inventory.parse_list_page(
            list_page("en", 1, 1, [list_row(8610, "Aggiba, the Malevolent Sh'nn S'yo", cnm=broken)]), "en")
        row = page["rows"][0]
        self.assertEqual((row["cid"], row["name"]), (8610, "Aggiba, the Malevolent Sh'nn S'yo"))
        self.assertEqual(row["info"], {"attribute": "SPELL", "text": "Line one.\nLine two."})

    def test_cid_and_link_must_agree(self):
        row = list_row(7, "A").replace('value="7"', 'value="8"')
        with self.assertRaisesRegex(InventoryError, "differs"):
            inventory.parse_list_page(list_page("en", 1, 1, [row]), "en")


class CrawlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.snapshot = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def site(self, locale, total, cids_by_page):
        pages, start = {}, 1
        for number, cids in enumerate(cids_by_page, 1):
            pages[(locale, number)] = list_page(locale, total, start, [list_row(c, f"Card {c}") for c in cids])
            start += len(cids)
        return pages

    def crawl(self, pages, locale="en"):
        return inventory.crawl_locale(locale, self.snapshot, lambda: FakeSession(pages))

    def test_two_pages_are_complete(self):
        with unittest.mock.patch.object(inventory, "PAGE_SIZE", 2):
            rows, summary = self.crawl(self.site("en", 3, [[1, 2], [3]]))
        self.assertEqual(sorted(rows), [1, 2, 3])
        self.assertEqual((summary["reported_total"], summary["pages"], summary["unique_cids"]), (3, 2, 3))

    def test_short_page_fails(self):
        with unittest.mock.patch.object(inventory, "PAGE_SIZE", 2):
            pages = self.site("en", 4, [[1, 2], [3]])
            with self.assertRaisesRegex(InventoryError, "shows 3-3, expected 3-4"):
                self.crawl(pages)

    def test_duplicate_across_pages_fails(self):
        with unittest.mock.patch.object(inventory, "PAGE_SIZE", 2):
            with self.assertRaisesRegex(InventoryError, "cid 2 is listed on page 1 and page 2"):
                self.crawl(self.site("en", 3, [[1, 2], [2]]))

    def test_page_without_rows_fails(self):
        with unittest.mock.patch.object(inventory, "PAGE_SIZE", 2):
            pages = self.site("en", 3, [[1, 2], [3]])
            pages[("en", 2)] = list_page("en", 3, 3, []).replace("3 - 2", "3 - 3")
            with self.assertRaisesRegex(InventoryError, "has 0 rows"):
                self.crawl(pages)

    def test_resume_reuses_checkpoints_and_rejects_a_changed_site(self):
        with unittest.mock.patch.object(inventory, "PAGE_SIZE", 2):
            pages = self.site("en", 3, [[1, 2], [3]])
            pages[("en", 2)] = requests.ConnectionError("reset")
            with self.assertRaises(requests.ConnectionError):
                self.crawl(pages)
            self.assertTrue((self.snapshot / "en" / "page_001.json").exists())
            self.assertFalse((self.snapshot / "en" / "page_002.json").exists())

            changed = self.site("en", 4, [[1, 2], [3, 4]])
            changed[("en", 1)] = RuntimeError("page 1 must come from the checkpoint")
            with self.assertRaisesRegex(InventoryError, "site changed mid-snapshot"):
                self.crawl(changed)

            resumed = self.site("en", 3, [[1, 2], [3]])
            resumed[("en", 1)] = RuntimeError("page 1 must come from the checkpoint")
            rows, summary = self.crawl(resumed)
        self.assertEqual((sorted(rows), summary["reused_checkpoint_pages"]), ([1, 2, 3], 1))


def official_card(cid, en=None, ja=None, ko=None):
    return {"cid": cid, "name_en": en, "name_ja": ja, "name_ko": ko, "info": {}}


def ygo_card(card_id, name, konami_id, image_ids=()):
    return {"id": card_id, "name": name, "misc_info": [{} if konami_id is None else {"konami_id": konami_id}],
            "card_images": [{"id": i} for i in (card_id, *image_ids)]}


def labels(*entries):
    return {str(n): {"card_id": str(card_id), "EN": en, "JA": None} for n, (card_id, en) in enumerate(entries)}


class MappingTests(unittest.TestCase):
    def test_image_alias_passcode_is_supported_and_other_cids_unsupported(self):
        official = {4041: official_card(4041, "Dark Magician"), 22723: official_card(22723, ja="スカーレッド"),
                    99999: official_card(99999, "Only On The Official Site")}
        ygo = [ygo_card(46986420, "Dark Magician", 4041, image_ids=[46986414]),
               ygo_card(65541655, "Red Nova Dragon - Burning Soul", 22723)]
        mappings, audit = inventory.map_cards(official, ygo, labels((46986414, "Dark Magician")))
        self.assertEqual((mappings[4041]["status"], mappings[4041]["draw2_card_ids"]), ("supported", [46986414]))
        self.assertEqual(mappings[22723]["status"], "unsupported")
        self.assertEqual(mappings[22723]["ygoprodeck_id"], 65541655)
        self.assertEqual((mappings[99999]["status"], mappings[99999]["method"]),
                         ("unsupported", "cid_not_in_draw2_identity_set"))
        self.assertEqual(audit["unplaced_draw2_identities"], [])

    def test_identity_without_cid_is_placed_by_exact_official_en_name(self):
        official = {5000: official_card(5000, "Get Your Game On!"), 5001: official_card(5001, "Other")}
        ygo = [ygo_card(111000561, "Get Your Game On!", None)]
        mappings, _ = inventory.map_cards(official, ygo, labels((111000561, "Get Your Game On!")))
        self.assertEqual((mappings[5000]["status"], mappings[5000]["method"]),
                         ("supported", "ygoprodeck_record_exact_official_en_name"))
        self.assertEqual(mappings[5001]["status"], "unsupported")

    def test_unplaced_identity_makes_negatives_unresolved(self):
        official = {5000: official_card(5000, "Something Else"), 4041: official_card(4041, "Dark Magician")}
        ygo = [ygo_card(222, "Prize Card", None), ygo_card(46986420, "Dark Magician", 4041)]
        mappings, audit = inventory.map_cards(official, ygo, labels((222, "Prize Card"), (46986420, "Dark Magician")))
        self.assertEqual(mappings[4041]["status"], "supported")
        self.assertEqual(mappings[5000]["status"], "unresolved")
        self.assertEqual(len(audit["unplaced_draw2_identities"]), 1)

    def test_verified_outside_official_db_identity_does_not_block_negatives(self):
        official = {5000: official_card(5000, "Something Else")}
        ygo = [ygo_card(111000561, "Get Your Game On!", None)]
        mappings, audit = inventory.map_cards(official, ygo, labels((111000561, "Get Your Game On!")))
        self.assertEqual(mappings[5000]["status"], "unsupported")
        self.assertEqual(audit["draw2_identities_outside_official_db"][0]["passcodes"], [111000561])

    def test_ambiguous_name_placement_is_not_guessed(self):
        official = {5000: official_card(5000, "Twin"), 5001: official_card(5001, "Twin")}
        ygo = [ygo_card(111000561, "Twin", None)]
        mappings, audit = inventory.map_cards(official, ygo, labels((111000561, "Twin")))
        self.assertEqual({m["status"] for m in mappings.values()}, {"unresolved"})
        self.assertIn("2 official EN list names", audit["unplaced_draw2_identities"][0]["reason"])

    def test_official_name_equal_to_an_unexplained_label_is_unresolved(self):
        official = {4041: official_card(4041, "Dark Magician"), 7000: official_card(7000, "Dark Magician Girl")}
        ygo = [ygo_card(46986420, "Dark Magician", 4041), ygo_card(38033121, "DMG", 4766)]
        mappings, _ = inventory.map_cards(official, ygo, labels((46986420, "Dark Magician"), (38033121, "Dark Magician Girl")))
        self.assertEqual(mappings[7000]["status"], "unresolved")  # label 38033121 sits on CID 4766, not listed

    def test_name_shared_by_two_official_cards_follows_the_konami_id(self):
        # CID 19092 (Normal) and CID 4370 (Ritual) are both "カオス・ソルジャー"; DRAW2 5405694 is CID 4370.
        official = {4370: official_card(4370, "Black Luster Soldier", ja="カオス・ソルジャー"),
                    19092: official_card(19092, ja="カオス・ソルジャー")}
        ygo = [ygo_card(5405694, "Black Luster Soldier", 4370)]
        model = {"0": {"card_id": "5405694", "EN": "Black Luster Soldier", "JA": "カオス・ソルジャー"}}
        mappings, _ = inventory.map_cards(official, ygo, model)
        self.assertEqual((mappings[4370]["status"], mappings[19092]["status"]), ("supported", "unsupported"))
        self.assertIn("CID 4370", mappings[19092]["note"])

    def test_konami_id_on_two_records_fails(self):
        ygo = [ygo_card(1, "A", 10), ygo_card(2, "B", 10)]
        with self.assertRaisesRegex(InventoryError, "konami_ids on several records"):
            inventory.map_cards({10: official_card(10, "A")}, ygo, labels((1, "A")))


def detail_page(cid, name, ciids):
    script = "".join(f"$('#card_image_{c}').attr('src', '/yugiohdb/get_image.action?type=2&cid={cid}&ciid={c}&enc=E{c}');\n"
                     for c in ciids)
    return f"<html><script>{script}</script><div id='cardname'><h1>{name}</h1></div></html>"


def image_url(cid, ciid):
    return f"{catalog.SITE}/yugiohdb/get_image.action?type=2&cid={cid}&ciid={ciid}&enc=E{ciid}"


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.out = Path(self.temp.name)
        self.card = {**official_card(22723, en=None, ja="スカーレッド", ko="스카레드"),
                     "mapping": {"status": "unsupported", "method": "ygoprodeck_konami_id", "passcodes": [65541655]}}
        # ja lists ciids 1 and 2; ko additionally lists ciid 3.
        self.pages = {catalog.detail_url(22723, "ja"): detail_page(22723, "スカーレッド", [1, 2]),
                      catalog.detail_url(22723, "ko"): detail_page(22723, "스카레드", [1, 3]),
                      image_url(22723, 1): png((200, 0, 0)), image_url(22723, 2): png((0, 200, 0)),
                      image_url(22723, 3): png((0, 0, 200))}

    def tearDown(self):
        self.temp.cleanup()

    def export(self):
        return inventory.export_missing_artworks([self.card], "snap", self.out, lambda: FakeSession(self.pages))

    def test_exports_union_of_locale_artworks_in_reference_schema(self):
        report = self.export()
        self.assertEqual((report["status"], report["cards_exported"], report["artworks_exported"]), ("complete", 1, 3))
        index = references.load_index(self.out)
        entry = index["cards"]["22723"]
        self.assertEqual([a["ciid"] for a in entry["artworks"]], [1, 2, 3])
        self.assertEqual(entry["source_url"], catalog.detail_url(22723, "ja"))
        self.assertEqual((entry["name_en"], entry["name_ko"]), (None, "스카레드"))

    def test_failed_image_is_not_published(self):
        self.pages[image_url(22723, 3)] = requests.HTTPError("503")
        report = self.export()
        self.assertEqual(report["status"], "incomplete")
        self.assertIn("HTTPError", report["failures"][0]["error"])
        self.assertFalse((self.out / "index.json").exists())
        self.assertFalse((self.out / "checkpoints" / "22723.json").exists())

    def test_placeholder_only_card_is_incomplete(self):
        placeholder = png((9, 9, 9))
        self.pages.update({image_url(22723, c): placeholder for c in (1, 2, 3)})
        with unittest.mock.patch.object(catalog, "PLACEHOLDER_IMAGE_SHA256",
                                        __import__("hashlib").sha256(placeholder).hexdigest()):
            report = self.export()
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["cards_without_artwork"][0]["placeholder_ciids"], [1, 2, 3])
        self.assertFalse((self.out / "index.json").exists())
        self.assertEqual(list((self.out / "images").glob("*")) if (self.out / "images").exists() else [], [])

    def test_resume_validates_checkpoint_images_and_mapping(self):
        self.export()
        for url in list(self.pages):
            self.pages[url] = RuntimeError("must resume from the checkpoint")
        self.assertEqual(self.export()["checkpoints_reused"], 1)

        image = next((self.out / "images").glob("22723_1_*"))
        image.write_bytes(png((1, 2, 3)))
        report = self.export()
        self.assertEqual(report["status"], "incomplete")
        self.assertIn("does not match its sha256", report["failures"][0]["error"])

        self.card["mapping"]["passcodes"] = [1]
        self.assertIn("different snapshot or mapping", self.export()["failures"][0]["error"])


class CodexCompletionTests(unittest.TestCase):
    """Codex-authored requirements: incomplete identification cannot report full success."""

    def test_unresolved_card_makes_full_command_fail_even_if_downloads_succeed(self):
        manifest = {"locales": {}, "union_cids": 1, "database": {},
                    "draw2_status_counts": {"unresolved": 1}}
        card = {**official_card(90001, "Unresolved Card"), "mapping": {"status": "unresolved"}}
        report = {"status": "complete", "failures": [], "cards_without_artwork": [],
                  "cards_with_unavailable_ciids": []}
        with unittest.mock.patch.object(sys, "argv", ["inventory.py", "--snapshot", "test"]), \
                unittest.mock.patch.object(inventory, "build_inventory", return_value=([card], manifest)), \
                unittest.mock.patch.object(inventory, "export_missing_artworks", return_value=report), \
                unittest.mock.patch.object(inventory, "write_json"), \
                self.assertRaises(SystemExit) as failure:
            inventory.main()
        self.assertNotIn(failure.exception.code, (None, 0))

    def test_snapshot_must_name_a_child_directory(self):
        for snapshot in (".", "..", "../outside", "..\\outside"):
            with self.subTest(snapshot=snapshot):
                with unittest.mock.patch.object(sys, "argv", ["inventory.py", "--snapshot", snapshot]), \
                        unittest.mock.patch.object(inventory, "build_inventory") as build, \
                        self.assertRaises(SystemExit) as failure:
                    inventory.main()
                self.assertNotIn(failure.exception.code, (None, 0))
                build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
