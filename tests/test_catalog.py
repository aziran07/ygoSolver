"""Offline tests for catalog.py. HTML fixtures mirror markup verified on db.yugioh-card.com (2026-10-07)."""

import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import catalog  # noqa: E402
from catalog import CatalogError  # noqa: E402


def search_html(rows, total=None):
    if not rows:
        return '<html><div class="no_data">No results</div></html>'
    body = "".join(
        f"""<div class="t_row c_normal open"><span class="card_name">\n{name}\n</span>
        <input type="hidden" class="cnm" value='{name}'>
        <input type="hidden" class="link_value" value="/yugiohdb/card_search.action?ope=2&cid={cid}"></div>"""
        for name, cid in rows
    )
    total = len(rows) if total is None else total
    return f'<html><div class="sort_set">Search Results: 1 - {len(rows)} of {total}</div>{body}</html>'


def detail_html(heading_inner, cid=4007):
    return f"""<html><script>
    $('#card_image_1').attr('src', '/yugiohdb/get_image.action?type=2&cid={cid}&ciid=1&enc=ABC_def-1').show();
    </script><div id="cardname" class="pc cardname"><h1>{heading_inner}</h1></div></html>"""


NO_DATA_DETAIL = '<html><div class="no_data" >\n카드 정보가 없습니다.\n</div></html>'


def png_bytes(color="red"):
    buffer = io.BytesIO()
    Image.new("RGB", (4, 6), color).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeResponse:
    def __init__(self, body, content_type="text/html;charset=UTF-8", status=200, url="fake"):
        self.content = body if isinstance(body, bytes) else body.encode("utf-8")
        self.text = body if isinstance(body, str) else ""
        self.headers = {"Content-Type": content_type}
        self.status_code = status
        self.url = url

    def raise_for_status(self):
        if self.status_code >= 400:
            raise catalog.requests.HTTPError(f"{self.status_code} for {self.url}")


class FakeSession:
    """Serves search pages, detail pages per locale, and one image; records every request."""

    def __init__(self, search_pages, details, image=None):
        self.search_pages = search_pages
        self.details = details
        self.image = image
        self.headers = {}
        self.requests = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.closed = True

    def get(self, url, params=None, headers=None, timeout=None):
        self.requests.append((url, params, headers))
        if "get_image.action" in url:
            return FakeResponse(self.image, content_type="application/octet-stream;charset=utf-8")
        if params is not None:
            return FakeResponse(self.search_pages[params["page"]])
        locale = url.rsplit("request_locale=", 1)[1]
        return FakeResponse(self.details[locale])


BLUE_EYES_SEARCH = search_html([
    ("Blue-Eyes Alternative White Dragon", 12253),
    ("Blue-Eyes White Dragon", 4007),
    ("Malefic Blue-Eyes White Dragon", 8864),
])
BLUE_EYES_DETAILS = {
    "en": detail_html("\nBlue-Eyes White Dragon\n"),
    "ko": detail_html("\n푸른 눈의 백룡\n<span>Blue-Eyes White Dragon</span>\n"),
    "ja": detail_html('<span class="ruby">ブルーアイズ・ホワイト・ドラゴン</span>\n青眼の白龍\n'
                      "<span>Blue-Eyes White Dragon</span>"),
}


class ParserTests(unittest.TestCase):
    def test_search_rows_and_total(self):
        html = search_html([("Ash Blossom &amp; Joyous Spring", 12950)], total=1)
        self.assertEqual(catalog.parse_search_page(html), (1, [("Ash Blossom & Joyous Spring", 12950)]))

    def test_search_preserves_names_with_apostrophes(self):
        # Konami's single-quoted input.cnm value is unescaped; the visible name is intact.
        for name, cid in [("Evil★Twin's Trouble Sunny", 16537),
                          ("Aggiba, the Malevolent Sh'nn S'yo", 8610)]:
            with self.subTest(name=name):
                self.assertEqual(catalog.parse_search_page(search_html([(name, cid)])),
                                 (1, [(name, cid)]))

    def test_search_without_rows_or_no_data_fails(self):
        with self.assertRaisesRegex(CatalogError, "neither result rows nor div.no_data"):
            catalog.parse_search_page("<html><body>maintenance</body></html>")

    def test_search_no_data_means_zero_results(self):
        self.assertEqual(catalog.parse_search_page(search_html([])), (0, []))

    def test_search_without_total_fails(self):
        html = search_html([("Blue-Eyes White Dragon", 4007)]).replace("Search Results: 1 - 1 of 1", "")
        with self.assertRaisesRegex(CatalogError, "no 'Search Results"):
            catalog.parse_search_page(html)

    def test_search_name_link_mismatch_fails(self):
        html = search_html([("Blue-Eyes White Dragon", 4007)]) + (
            '<div class="t_row c_normal open"><span class="card_name">x</span></div>')
        with self.assertRaises(CatalogError):
            catalog.parse_search_page(html)

    def test_search_rejects_missing_empty_or_duplicate_visible_name(self):
        html = search_html([("Blue-Eyes White Dragon", 4007)])
        visible_name = '<span class="card_name">\nBlue-Eyes White Dragon\n</span>'
        for replacement in ("", '<span class="card_name"> \n </span>', visible_name * 2):
            with self.subTest(replacement=replacement), self.assertRaises(CatalogError):
                catalog.parse_search_page(html.replace(visible_name, replacement))

    def test_search_does_not_pair_names_and_links_across_rows(self):
        html = '''<div class="sort_set">Search Results: 1 - 2 of 2</div>
        <div class="t_row c_normal open"><span class="card_name">First</span>
          <input class="cnm" value="First"></div>
        <div class="t_row c_normal open"><span class="card_name">Second</span>
          <input class="cnm" value="Second">
          <input class="link_value" value="?ope=2&amp;cid=1">
          <input class="link_value" value="?ope=2&amp;cid=2"></div>'''
        with self.assertRaises(CatalogError):
            catalog.parse_search_page(html)

    def test_detail_names_exclude_ruby_and_english_spans(self):
        self.assertEqual(catalog.parse_detail_page(BLUE_EYES_DETAILS["ko"])[0], "푸른 눈의 백룡")
        self.assertEqual(catalog.parse_detail_page(BLUE_EYES_DETAILS["ja"])[0], "青眼の白龍")
        name, image_src = catalog.parse_detail_page(BLUE_EYES_DETAILS["en"])
        self.assertEqual(name, "Blue-Eyes White Dragon")
        self.assertEqual(image_src, "/yugiohdb/get_image.action?type=2&cid=4007&ciid=1&enc=ABC_def-1")

    def test_detail_no_data_is_unavailable_locale(self):
        self.assertEqual(catalog.parse_detail_page(NO_DATA_DETAIL), (None, None))

    def test_detail_unexpected_html_fails(self):
        with self.assertRaisesRegex(CatalogError, "neither #cardname nor div.no_data"):
            catalog.parse_detail_page("<html><h1>Access denied</h1></html>")

    def test_detail_without_image_script_fails(self):
        html = '<div id="cardname"><h1>Blue-Eyes White Dragon</h1></div>'
        with self.assertRaisesRegex(CatalogError, "no \\$\\('#card_image_1'\\)"):
            catalog.parse_detail_page(html)

    def test_detail_image_without_enc_fails(self):
        html = BLUE_EYES_DETAILS["en"].replace("&enc=ABC_def-1", "")
        with self.assertRaisesRegex(CatalogError, "no enc parameter"):
            catalog.parse_detail_page(html)


class ExactNameTests(unittest.TestCase):
    def test_verified_reactor_model_labels_match_official_names(self):
        for label, official, cid in [("Spell Reactor RE", "Spell Reactor ・RE", 8002),
                                     ("Trap Reactor Y FI", "Trap Reactor ・Y FI", 8000),
                                     ("Summon Reactor SK", "Summon Reactor ・SK", 7998)]:
            with self.subTest(label=label):
                session = FakeSession({1: search_html([(official, cid)])}, {})
                self.assertEqual(catalog.find_cid(session, label), cid)
                self.assertEqual(catalog.find_cid(session, official), cid)

    def test_reactor_alias_does_not_relax_other_names_or_ambiguity(self):
        for wrong_name in ("Spell Reactor", "Spell Reactor ・SK", "Spell Reactor ☆RE",
                           "Spell Reactor ・RE Extra"):
            with self.subTest(wrong_name=wrong_name):
                session = FakeSession({1: search_html([(wrong_name, 8002)])}, {})
                with self.assertRaisesRegex(CatalogError, "no official card named exactly"):
                    catalog.find_cid(session, "Spell Reactor RE")
        session = FakeSession({1: search_html([("Spell Reactor ・RE", 8002),
                                               ("Spell Reactor ・RE", 9999)])}, {})
        with self.assertRaisesRegex(CatalogError, "several cids"):
            catalog.find_cid(session, "Spell Reactor RE")

    def test_apostrophe_name_matches_its_official_cid(self):
        name = "Evil★Twin's Trouble Sunny"
        session = FakeSession({1: search_html([(name, 16537)])}, {})
        self.assertEqual(catalog.find_cid(session, name), 16537)

    def test_single_result_still_requires_exact_name_including_symbols(self):
        name = "Evil★Twin's Trouble Sunny"
        for different in ("Evil★Twin", "Evil☆Twin's Trouble Sunny", "Evil★Twins Trouble Sunny"):
            with self.subTest(different=different):
                session = FakeSession({1: search_html([(different, 16537)])}, {})
                with self.assertRaisesRegex(CatalogError, "no official card named exactly"):
                    catalog.find_cid(session, name)

    def test_picks_exact_name_among_substring_matches(self):
        session = FakeSession({1: BLUE_EYES_SEARCH}, {})
        self.assertEqual(catalog.find_cid(session, "blue-eyes  white dragon"), 4007)
        self.assertEqual(session.requests[0][1]["stype"], 1)
        self.assertEqual(session.requests[0][1]["request_locale"], "en")

    def test_partial_name_is_not_matched(self):
        session = FakeSession({1: BLUE_EYES_SEARCH}, {})
        with self.assertRaisesRegex(CatalogError, "no official card named exactly 'Blue-Eyes'"):
            catalog.find_cid(session, "Blue-Eyes")

    def test_same_name_with_two_cids_is_ambiguous(self):
        session = FakeSession({1: search_html([("Twin", 1), ("Twin", 2)])}, {})
        with self.assertRaisesRegex(CatalogError, r"several cids: \[1, 2\]"):
            catalog.find_cid(session, "Twin")

    def test_follows_pages_until_total(self):
        filler = [(f"Dragon {index}", index) for index in range(1, 101)]
        pages = {1: search_html(filler, total=101), 2: search_html([("Dark Magician", 4041)], total=101)}
        session = FakeSession(pages, {})
        self.assertEqual(catalog.find_cid(session, "Dark Magician"), 4041)
        self.assertEqual([request[1]["page"] for request in session.requests], [1, 2])


class ImageTests(unittest.TestCase):
    def test_rejects_placeholder_and_garbage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            stem = Path(temp_dir) / "card"
            placeholder = png_bytes("black")
            with mock.patch.object(catalog, "PLACEHOLDER_IMAGE_SHA256", hashlib.sha256(placeholder).hexdigest()):
                with self.assertRaisesRegex(CatalogError, "Coming Soon"):
                    catalog.download_image(FakeSession({}, {}, placeholder), "/yugiohdb/get_image.action?enc=x", "ref", stem)
            with self.assertRaisesRegex(CatalogError, "is not a decodable image"):
                catalog.download_image(FakeSession({}, {}, b"<html>error</html>"), "/yugiohdb/get_image.action?enc=x", "ref", stem)
            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    def test_saves_valid_png_with_referer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session = FakeSession({}, {}, png_bytes())
            path = catalog.download_image(session, "/yugiohdb/get_image.action?enc=x", "https://ref", Path(temp_dir) / "card")
            self.assertEqual(path.name, "card.png")
            self.assertEqual(path.read_bytes(), png_bytes())
            self.assertEqual(session.requests[0][2], {"Referer": "https://ref"})


class ResolveCardCacheTests(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.data_dir = Path(temp_dir.name)

    def resolve_with(self, session, name="Blue-Eyes White Dragon"):
        with mock.patch.object(catalog, "new_session", return_value=session):
            return catalog.resolve_card(name, self.data_dir)

    def test_resolves_caches_and_reuses(self):
        details = dict(BLUE_EYES_DETAILS, ko=NO_DATA_DETAIL)
        card = self.resolve_with(FakeSession({1: BLUE_EYES_SEARCH}, details, png_bytes()))
        self.assertEqual(card, {
            "cid": 4007,
            "name_en": "Blue-Eyes White Dragon",
            "name_ko": None,
            "name_ja": "青眼の白龍",
            "image_path": str(self.data_dir / "images" / "4007_en.png"),
            "source_url": catalog.detail_url(4007, "en"),
        })
        cache = json.loads((self.data_dir / "cards.json").read_text(encoding="utf-8"))
        self.assertEqual(cache["blue-eyes white dragon"]["image_path"], "images/4007_en.png")
        self.assertEqual([p.name for p in self.data_dir.iterdir() if p.suffix == ".tmp"], [])

        offline = FakeSession({}, {})
        self.assertEqual(self.resolve_with(offline, "BLUE-EYES WHITE DRAGON"), card)
        self.assertEqual(offline.requests, [])

    def test_reactor_alias_preserves_official_metadata_and_reuses_cache(self):
        official = "Spell Reactor ・RE"
        details = {"en": detail_html(official, cid=8002),
                   "ko": detail_html("매직 리액터 AID", cid=8002),
                   "ja": detail_html("マジック・リアクター・ＡＩＤ", cid=8002)}
        session = FakeSession({1: search_html([(official, 8002)])}, details, png_bytes())
        card = self.resolve_with(session, "Spell Reactor RE")
        self.assertEqual((card["cid"], card["name_en"], card["name_ko"]),
                         (8002, official, "매직 리액터 AID"))
        for name in ("spell reactor  re", official):
            offline = FakeSession({}, {})
            self.assertEqual(self.resolve_with(offline, name), card)
            self.assertEqual(offline.requests, [])
        cache_path = self.data_dir / "cards.json"
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        cache["spell reactor re"]["name_en"] = "Trap Reactor ・Y FI"
        cache_path.write_text(json.dumps(cache), encoding="utf-8")
        with self.assertRaisesRegex(CatalogError, "does not match its key"):
            self.resolve_with(FakeSession({}, {}), "Spell Reactor RE")

    def test_reactor_alias_rejects_different_detail_identity(self):
        session = FakeSession({1: search_html([("Spell Reactor ・RE", 8002)])},
                              {"en": detail_html("Trap Reactor ・Y FI", cid=8002)})
        with self.assertRaisesRegex(CatalogError, "detail name.*differs"):
            self.resolve_with(session, "Spell Reactor RE")
        self.assertFalse((self.data_dir / "cards.json").exists())

    def test_image_fetched_right_after_english_detail(self):
        session = FakeSession({1: BLUE_EYES_SEARCH}, BLUE_EYES_DETAILS, png_bytes())
        self.resolve_with(session)
        urls = [url for url, _, _ in session.requests]
        english_index = urls.index(catalog.detail_url(4007, "en"))
        self.assertIn("get_image.action", urls[english_index + 1])
        self.assertIn("enc=ABC_def-1", urls[english_index + 1])

    def test_failure_does_not_write_cache_and_names_url(self):
        session = FakeSession({1: BLUE_EYES_SEARCH}, dict(BLUE_EYES_DETAILS, ja="<html>blocked</html>"), png_bytes())
        with self.assertRaises(CatalogError) as raised:
            self.resolve_with(session)
        self.assertIn(catalog.detail_url(4007, "ja"), str(raised.exception))
        self.assertFalse((self.data_dir / "cards.json").exists())
        self.assertTrue(session.closed)

    def test_invalid_names_rejected_without_network(self):
        for bad_name, reason in [("   ", "blank"), (None, "must be a string"), ("x" * 201, "longer than 200")]:
            offline = FakeSession({}, {})
            with self.assertRaisesRegex(CatalogError, reason):
                self.resolve_with(offline, bad_name)
            self.assertEqual(offline.requests, [])

    def tamper_cache(self, **changes):
        self.resolve_with(FakeSession({1: BLUE_EYES_SEARCH}, BLUE_EYES_DETAILS, png_bytes()))
        cache_path = self.data_dir / "cards.json"
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        cache["blue-eyes white dragon"].update(changes)
        cache_path.write_text(json.dumps(cache), encoding="utf-8")

    def test_tampered_cache_entries_raise(self):
        cases = [({"cid": "4007"}, "invalid cid"), ({"cid": -1}, "invalid cid"),
                 ({"name_en": "Blue-Eyes Ultimate Dragon"}, "does not match its key"),
                 ({"name_ja": 5}, "non-string localized names"),
                 ({"source_url": "https://example.com"}, "unexpected source_url"),
                 ({"extra": 1}, "has fields")]
        for changes, reason in cases:
            with self.subTest(changes=changes):
                self.tamper_cache(**changes)
                with self.assertRaisesRegex(CatalogError, reason):
                    self.resolve_with(FakeSession({}, {}))
                (self.data_dir / "cards.json").unlink()

    def test_corrupt_cached_image_raises(self):
        self.resolve_with(FakeSession({1: BLUE_EYES_SEARCH}, BLUE_EYES_DETAILS, png_bytes()))
        image_path = self.data_dir / "images" / "4007_en.png"
        image_path.write_bytes(png_bytes()[:30])
        with self.assertRaisesRegex(CatalogError, "is not a decodable image"):
            self.resolve_with(FakeSession({}, {}))

    def test_http_error_propagates(self):
        class FailingSession(FakeSession):
            def get(self, url, params=None, headers=None, timeout=None):
                return FakeResponse("busy", status=503)
        with self.assertRaises(catalog.requests.HTTPError):
            self.resolve_with(FailingSession({}, {}))

    def test_unknown_name_raises(self):
        with self.assertRaisesRegex(CatalogError, "no official card named exactly"):
            self.resolve_with(FakeSession({1: search_html([])}, {}), "Not A Card")

    def test_corrupt_cache_raises(self):
        (self.data_dir / "cards.json").write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(CatalogError, "not valid JSON"):
            self.resolve_with(FakeSession({}, {}))

    def test_cached_entry_with_missing_image_raises(self):
        self.resolve_with(FakeSession({1: BLUE_EYES_SEARCH}, BLUE_EYES_DETAILS, png_bytes()))
        (self.data_dir / "images" / "4007_en.png").unlink()
        with self.assertRaisesRegex(CatalogError, "image is missing"):
            self.resolve_with(FakeSession({}, {}))


if __name__ == "__main__":
    unittest.main()
