"""Regression for the set_5 DRAW2 label, verified against Konami CID 22992."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import catalog
import references
from test_catalog import FakeResponse, FakeSession, detail_html, png_bytes, search_html
from test_references import LibraryTestCase


class ZennaSession(FakeSession):
    def __init__(self):
        super().__init__({1: search_html([("Zenna's Deceiving Doll Maidens", 22992)])}, {
            "en": detail_html("Zenna's Deceiving Doll Maidens", cid=22992),
            "ko": detail_html("젠나노츠이나기히메", cid=22992),
            "ja": detail_html("ぜんなのついなぎひめ", cid=22992),
        }, png_bytes())

    def get(self, url, params=None, headers=None, timeout=None):
        if params is not None and params["keyword"] != "Zenna's Deceiving Doll Maidens":
            self.requests.append((url, params, headers))
            return FakeResponse(search_html([]))
        return super().get(url, params, headers, timeout)


class ZennaCatalogTests(unittest.TestCase):
    def test_model_and_official_names_search_exact_official_name_first(self):
        for name in ("Zenna-no-Tsuinagihime", " ZENNA-NO-TSUINAGIHIME ",
                     "Zenna's Deceiving Doll Maidens"):
            with self.subTest(name=name):
                session = ZennaSession()
                self.assertEqual(catalog.find_cid(session, name), 22992)
                self.assertEqual([r[1]["keyword"] for r in session.requests],
                                 ["Zenna's Deceiving Doll Maidens"])

    def test_cache_shares_original_model_key_and_validates_identity(self):
        for first in ("Zenna-no-Tsuinagihime", "Zenna's Deceiving Doll Maidens"):
            with self.subTest(first=first), tempfile.TemporaryDirectory() as directory:
                with mock.patch.object(catalog, "new_session", return_value=ZennaSession()):
                    card = catalog.resolve_card(first, directory)
                self.assertEqual((card["cid"], card["name_en"], card["name_ko"], card["name_ja"]),
                                 (22992, "Zenna's Deceiving Doll Maidens", "젠나노츠이나기히메",
                                  "ぜんなのついなぎひめ"))
                with mock.patch.object(catalog, "new_session", side_effect=AssertionError("unexpected network")):
                    for name in ("Zenna-no-Tsuinagihime", "Zenna's Deceiving Doll Maidens"):
                        self.assertEqual(catalog.resolve_card(name, directory), card)
                cache_path = Path(directory) / "cards.json"
                cache = json.loads(cache_path.read_text(encoding="utf-8"))
                self.assertEqual(list(cache), ["zenna-no-tsuinagihime"])
                cache["zenna-no-tsuinagihime"]["name_en"] = "Different Card"
                cache_path.write_text(json.dumps(cache), encoding="utf-8")
                with self.assertRaisesRegex(catalog.CatalogError, "does not match its key"):
                    catalog.resolve_card("Zenna-no-Tsuinagihime", directory)

    def test_similar_names_and_ambiguous_cids_still_fail(self):
        for wrong in ("Zenna-no-Tsuinagihime Extra", "Zenna no Tsuinagihime",
                      "Zennas Deceiving Doll Maidens", "Zenna's Deceiving Doll Maidens Extra"):
            with self.subTest(wrong=wrong):
                session = ZennaSession()
                session.search_pages[1] = search_html([(wrong, 22992)])
                with self.assertRaisesRegex(catalog.CatalogError, "no official card named exactly"):
                    catalog.find_cid(session, "Zenna-no-Tsuinagihime")
                session = FakeSession({1: search_html([("Zenna's Deceiving Doll Maidens", 22992)])}, {})
                with self.assertRaisesRegex(catalog.CatalogError, "no official card named exactly"):
                    catalog.find_cid(session, wrong)
        session = ZennaSession()
        session.search_pages[1] = search_html([("Zenna's Deceiving Doll Maidens", 22992),
                                             ("Zenna's Deceiving Doll Maidens", 99999)])
        with self.assertRaisesRegex(catalog.CatalogError, "several cids"):
            catalog.find_cid(session, "Zenna-no-Tsuinagihime")

    def test_wrong_detail_identity_does_not_create_success_cache(self):
        session = ZennaSession()
        session.details["en"] = detail_html("Different Card", cid=22992)
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(catalog, "new_session", return_value=session):
            with self.assertRaisesRegex(catalog.CatalogError, "detail name.*differs"):
                catalog.resolve_card("Zenna-no-Tsuinagihime", directory)
            self.assertFalse((Path(directory) / "cards.json").exists())

    def test_http_failure_propagates_without_retry(self):
        session = ZennaSession()
        with mock.patch.object(session, "get", return_value=FakeResponse("unavailable", status=503)) as get:
            with self.assertRaises(catalog.requests.HTTPError):
                catalog.find_cid(session, "Zenna-no-Tsuinagihime")
        get.assert_called_once()
        self.assertEqual(get.call_args.kwargs["params"]["keyword"], "Zenna's Deceiving Doll Maidens")


class ZennaCandidateTests(LibraryTestCase):
    def test_candidate_review_resolves_model_label_without_inventing_visual_match(self):
        self.add_card(22992, {1: 22}, name_en="Zenna's Deceiving Doll Maidens",
                      name_ko="젠나노츠이나기히메")
        candidates = [{"card_id": "7594154", "name_en": "Zenna-no-Tsuinagihime", "score": .02}]
        resolve = catalog.resolve_card
        with mock.patch.object(catalog, "new_session", return_value=ZennaSession()), \
                mock.patch.object(catalog, "resolve_card", side_effect=lambda name: resolve(name, self.root / "catalog")):
            result = references.CandidateReviewer(self.data_dir).review(Image.new("RGB", (240, 340)), candidates)
        self.assertFalse(result["accepted"])
        candidate = result["candidates"][0]
        self.assertEqual((candidate["name_en"], candidate["score"]), ("Zenna-no-Tsuinagihime", .02))
        self.assertEqual((candidate["review"]["cid"], candidate["review"]["card"]["name_en"]),
                         (22992, "Zenna's Deceiving Doll Maidens"))
        self.assertEqual(candidate["review"]["outcome"], "none")


if __name__ == "__main__":
    unittest.main()
