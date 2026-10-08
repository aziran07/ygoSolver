"""Regression for DRAW2's Babalon label versus Konami's Baybarron (CID 22980)."""

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


class BaybarronSession(FakeSession):
    """The reported spelling really yields zero results; only the official query works."""

    def __init__(self):
        super().__init__({1: search_html([("Invoked Baybarron", 22980)])}, {
            "en": detail_html("Invoked Baybarron", cid=22980),
            "ko": detail_html("소환수 베이발론", cid=22980),
            "ja": detail_html("召喚獣ベイバロン", cid=22980),
        }, png_bytes())

    def get(self, url, params=None, headers=None, timeout=None):
        if params is not None and params["keyword"] != "Invoked Baybarron":
            self.requests.append((url, params, headers))
            return FakeResponse(search_html([]))
        return super().get(url, params, headers, timeout)


class BabalonCatalogTests(unittest.TestCase):
    def test_verified_alias_queries_official_name_before_any_request(self):
        for name in ("Invoked Babalon", " INVOKED   BABALON ", "Invoked Baybarron"):
            with self.subTest(name=name):
                session = BaybarronSession()
                self.assertEqual(catalog.find_cid(session, name), 22980)
                self.assertEqual([r[1]["keyword"] for r in session.requests], ["Invoked Baybarron"])

    def test_alias_and_official_name_share_cache_and_keep_official_metadata(self):
        for first in ("Invoked Babalon", "Invoked Baybarron"):
            with self.subTest(first=first), tempfile.TemporaryDirectory() as directory:
                with mock.patch.object(catalog, "new_session", return_value=BaybarronSession()):
                    card = catalog.resolve_card(first, directory)
                self.assertEqual((card["cid"], card["name_en"], card["name_ko"], card["name_ja"]),
                                 (22980, "Invoked Baybarron", "소환수 베이발론", "召喚獣ベイバロン"))
                self.assertTrue(Path(card["image_path"]).is_file())
                with mock.patch.object(catalog, "new_session", side_effect=AssertionError("unexpected network")):
                    for name in ("Invoked Babalon", "Invoked Baybarron", " INVOKED  BABALON "):
                        self.assertEqual(catalog.resolve_card(name, directory), card)
                cache_path = Path(directory) / "cards.json"
                cache = json.loads(cache_path.read_text(encoding="utf-8"))
                self.assertEqual(list(cache), ["invoked babalon"], "preserve the old session lookup key")
                cache["invoked babalon"]["name_en"] = "Invoked Mechaba"
                cache_path.write_text(json.dumps(cache), encoding="utf-8")
                with self.assertRaisesRegex(catalog.CatalogError, "does not match its key"):
                    catalog.resolve_card("Invoked Babalon", directory)

    def test_similar_names_and_duplicate_cids_still_fail(self):
        for wrong in ("Invoked Babalon Extra", "Invoked Baybaron", "Invoked Baybarron Extra"):
            with self.subTest(wrong=wrong):
                session = BaybarronSession()
                session.search_pages[1] = search_html([(wrong, 22980)])
                with self.assertRaisesRegex(catalog.CatalogError, "no official card named exactly"):
                    catalog.find_cid(session, "Invoked Babalon")
                session = FakeSession({1: search_html([("Invoked Baybarron", 22980)])}, {})
                with self.assertRaisesRegex(catalog.CatalogError, "no official card named exactly"):
                    catalog.find_cid(session, wrong)
        session = BaybarronSession()
        session.search_pages[1] = search_html([("Invoked Baybarron", 22980), ("Invoked Baybarron", 99999)])
        with self.assertRaisesRegex(catalog.CatalogError, "several cids"):
            catalog.find_cid(session, "Invoked Babalon")

    def test_different_detail_identity_is_rejected_without_cache(self):
        session = BaybarronSession()
        session.details["en"] = detail_html("Invoked Mechaba", cid=22980)
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(catalog, "new_session", return_value=session):
            with self.assertRaisesRegex(catalog.CatalogError, "detail name.*differs"):
                catalog.resolve_card("Invoked Babalon", directory)
            self.assertFalse((Path(directory) / "cards.json").exists())

    def test_http_failure_is_not_retried_or_hidden(self):
        session = BaybarronSession()
        with mock.patch.object(session, "get", return_value=FakeResponse("unavailable", status=503)) as get:
            with self.assertRaises(catalog.requests.HTTPError):
                catalog.find_cid(session, "Invoked Babalon")
        get.assert_called_once()
        self.assertEqual(get.call_args.kwargs["params"]["keyword"], "Invoked Baybarron")


class BabalonCandidateTests(LibraryTestCase):
    def test_real_catalog_path_allows_artwork_review_without_inventing_confidence(self):
        self.add_card(22980, {1: 22}, name_en="Invoked Baybarron", name_ko="소환수 베이발론")
        candidates = [{"card_id": "70383419", "name_en": "Invoked Babalon", "score": .02}]
        resolve = catalog.resolve_card
        with mock.patch.object(catalog, "new_session", return_value=BaybarronSession()), \
                mock.patch.object(catalog, "resolve_card", side_effect=lambda name: resolve(name, self.root / "catalog")):
            result = references.CandidateReviewer(self.data_dir).review(Image.new("RGB", (240, 340)), candidates)
        self.assertFalse(result["accepted"], "a successful name lookup is not visual confirmation")
        candidate = result["candidates"][0]
        self.assertEqual((candidate["name_en"], candidate["score"]), ("Invoked Babalon", .02))
        self.assertEqual((candidate["review"]["cid"], candidate["review"]["card"]["name_en"]),
                         (22980, "Invoked Baybarron"))
        self.assertEqual(candidate["review"]["outcome"], "none")


if __name__ == "__main__":
    unittest.main()
