"""DRAW2's literal HTML entity must query the verified official ampersand name."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import catalog
from test_catalog import FakeResponse, FakeSession, detail_html, png_bytes, search_html

MODEL_NAME = "The Fallen &amp; The Virtuous"
OFFICIAL_NAME = "The Fallen & The Virtuous"


class FallenSession(FakeSession):
    def __init__(self):
        super().__init__({1: search_html([(OFFICIAL_NAME, 22089)])}, {
            "en": detail_html(OFFICIAL_NAME, cid=22089),
            "ko": detail_html(OFFICIAL_NAME, cid=22089),
            "ja": detail_html("The Fallen ＆ The Virtuous", cid=22089),
        }, png_bytes())

    def get(self, url, params=None, headers=None, timeout=None):
        if params is not None and params["keyword"] != OFFICIAL_NAME:
            self.requests.append((url, params, headers))
            return FakeResponse(search_html([]))
        return super().get(url, params, headers, timeout)


class FallenNameTests(unittest.TestCase):
    def test_encoded_label_queries_official_name_once_and_shares_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            session = FallenSession()
            with mock.patch.object(catalog, "new_session", return_value=session):
                card = catalog.resolve_card(" THE FALLEN  &amp; THE VIRTUOUS ", directory)
            self.assertEqual((card["cid"], card["name_en"], card["name_ko"]),
                             (22089, OFFICIAL_NAME, OFFICIAL_NAME))
            self.assertEqual([r[1]["keyword"] for r in session.requests if r[1]], [OFFICIAL_NAME])
            with mock.patch.object(catalog, "new_session", side_effect=AssertionError("unexpected network")):
                self.assertEqual(catalog.resolve_card(OFFICIAL_NAME, directory), card)
                self.assertEqual(catalog.resolve_card(MODEL_NAME, directory), card)
            cache_path = Path(directory) / "cards.json"
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(list(cache), ["the fallen &amp; the virtuous"])
            cache["the fallen &amp; the virtuous"]["name_en"] = "Fallen of Albaz"
            cache_path.write_text(json.dumps(cache), encoding="utf-8")
            with self.assertRaisesRegex(catalog.CatalogError, "does not match its key"):
                catalog.resolve_card(MODEL_NAME, directory)

    def test_alias_does_not_accept_different_names_or_ambiguous_identity(self):
        for wrong in ("The Fallen and The Virtuous", "The Fallen & The Virtuous Extra"):
            with self.subTest(wrong=wrong):
                session = FallenSession()
                session.search_pages[1] = search_html([(wrong, 22089)])
                with self.assertRaisesRegex(catalog.CatalogError, "no official card named exactly"):
                    catalog.find_cid(session, MODEL_NAME)
        session = FallenSession()
        session.search_pages[1] = search_html([(OFFICIAL_NAME, 22089), (OFFICIAL_NAME, 99999)])
        with self.assertRaisesRegex(catalog.CatalogError, "several cids"):
            catalog.find_cid(session, MODEL_NAME)
        session = FallenSession()
        session.details["en"] = detail_html("Fallen of Albaz", cid=22089)
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(catalog, "new_session", return_value=session):
            with self.assertRaisesRegex(catalog.CatalogError, "detail name.*differs"):
                catalog.resolve_card(MODEL_NAME, directory)
            self.assertFalse((Path(directory) / "cards.json").exists())


if __name__ == "__main__":
    unittest.main()
