"""Offline tests for references.py: synthetic card images and a fake HTTP session (no network, no data files)."""

import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import requests
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import catalog  # noqa: E402
import references  # noqa: E402
from references import ReferenceLibraryError  # noqa: E402

CARD_SIZE = (420, 610)


def artwork(seed):
    """Deterministic random-shape illustration (rich in SIFT features)."""
    rng = np.random.default_rng(seed)
    art = Image.new("RGB", (320, 260), tuple(int(v) for v in rng.integers(0, 256, 3)))
    draw = ImageDraw.Draw(art)
    for _ in range(90):
        x, y = rng.integers(0, 320), rng.integers(0, 260)
        w, h = rng.integers(8, 60, 2)
        color = tuple(int(v) for v in rng.integers(0, 256, 3))
        if rng.random() < 0.5:
            draw.ellipse([x, y, x + w, y + h], fill=color)
        else:
            draw.rectangle([x, y, x + w, y + h], fill=color)
    return art


def card_image(art_seed, frame=(200, 120, 60), title_seed=0, right_part_seed=None):
    """A card: frame, title bar and text box around the art window used by references.py.

    right_part_seed replaces the right 40% of the illustration (a partly shared artwork).
    """
    card = Image.new("RGB", CARD_SIZE, frame)
    draw = ImageDraw.Draw(card)
    rng = np.random.default_rng(1000 + title_seed)
    for _ in range(12):  # "title text" and "effect text" strokes
        x = int(rng.integers(20, 360))
        draw.rectangle([x, 30, x + 25, 60], fill=(0, 0, 0))
        y = int(rng.integers(470, 570))
        draw.rectangle([x, y, x + 40, y + 10], fill=(30, 30, 30))
    left, top = round(0.08 * CARD_SIZE[0]), round(0.16 * CARD_SIZE[1])
    right, bottom = round(0.92 * CARD_SIZE[0]), round(0.66 * CARD_SIZE[1])
    art = artwork(art_seed)
    if right_part_seed is not None:
        art.paste(artwork(right_part_seed).crop((192, 0, 320, 260)), (192, 0))
    card.paste(art.resize((right - left, bottom - top)), (left, top))
    return card


def png_bytes(image):
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def photo_crop(card, upside_down=False, frame=(40, 90, 200)):
    """Simulate a DRAW2 crop: different frame/text (other locale), smaller, optionally upside down, noisy."""
    rgb = np.asarray(card).copy()
    height, width = rgb.shape[:2]
    rgb[: round(0.12 * height)] = frame
    rgb[round(0.70 * height):] = frame
    image = Image.fromarray(rgb).resize((235, 340), Image.BILINEAR)
    if upside_down:
        image = image.rotate(180)
    noisy = np.asarray(image).astype(np.int16) + np.random.default_rng(5).integers(-6, 7, (340, 235, 3))
    return Image.fromarray(np.clip(noisy, 0, 255).astype(np.uint8))


def card_record(cid, name_en="Card", name_ja=None, name_ko=None, locale="en"):
    return {"cid": cid, "name_en": name_en, "name_ja": name_ja, "name_ko": name_ko,
            "source_url": catalog.detail_url(cid, locale)}


class LibraryTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.data_dir = self.root / "references"

    def image_file(self, image, name):
        path = self.root / name
        path.write_bytes(png_bytes(image))
        return path

    def add_card(self, cid, art_seeds, right_part_seed=None, **names):
        files = {ciid: self.image_file(card_image(seed, right_part_seed=right_part_seed), f"{cid}_{ciid}.png")
                 for ciid, seed in art_seeds.items()}
        return references.upsert_card(self.data_dir, card_record(cid, **names), files)


class LibraryInfoTests(LibraryTestCase):
    def test_none_only_when_not_initialized(self):
        self.assertIsNone(references.library_info(self.data_dir))

    def test_counts_and_fingerprint_follow_registered_content(self):
        self.add_card(101, {1: 1, 2: 2})
        self.add_card(102, {1: 3})
        info = references.library_info(self.data_dir)
        self.assertEqual((info["card_count"], info["artwork_count"]), (2, 3))
        self.add_card(102, {1: 4})  # re-enrolled with a different artwork
        changed = references.library_info(self.data_dir)
        self.assertEqual((changed["card_count"], changed["artwork_count"]), (2, 3))
        self.assertNotEqual(info["fingerprint"], changed["fingerprint"])

    def test_corrupt_index_raises(self):
        self.data_dir.mkdir()
        (self.data_dir / "index.json").write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(ReferenceLibraryError, "not valid JSON"):
            references.library_info(self.data_dir)
        (self.data_dir / "index.json").write_text(json.dumps({"schema": 99, "cards": {}}), encoding="utf-8")
        with self.assertRaisesRegex(ReferenceLibraryError, "schema"):
            references.library_info(self.data_dir)

    def test_missing_or_altered_image_raises(self):
        entry = self.add_card(101, {1: 1})
        image_path = self.data_dir / entry["artworks"][0]["image"]
        image_path.write_bytes(png_bytes(card_image(9)))
        with self.assertRaisesRegex(ReferenceLibraryError, "sha256"):
            references.library_info(self.data_dir)
        image_path.unlink()
        with self.assertRaisesRegex(ReferenceLibraryError, "missing"):
            references.library_info(self.data_dir)
        with self.assertRaisesRegex(ReferenceLibraryError, "missing"):
            references.ReferenceMatcher(self.data_dir)

    def test_index_paths_cannot_escape_library(self):
        self.add_card(101, {1: 1})
        index = json.loads((self.data_dir / "index.json").read_text(encoding="utf-8"))
        index["cards"]["101"]["artworks"][0]["image"] = "../outside.png"
        (self.data_dir / "index.json").write_text(json.dumps(index), encoding="utf-8")
        with self.assertRaisesRegex(ReferenceLibraryError, "invalid image path"):
            references.library_info(self.data_dir)

    def test_card_needs_some_official_name(self):
        with self.assertRaisesRegex(ReferenceLibraryError, "no name"):
            self.add_card(101, {1: 1}, name_en=None)
        with self.assertRaisesRegex(ReferenceLibraryError, "non-blank"):
            self.add_card(101, {1: 1}, name_en=" ", name_ja="", name_ko="")

    def test_schema_must_be_the_integer_version(self):
        self.data_dir.mkdir()
        (self.data_dir / "index.json").write_text(json.dumps({"schema": True, "cards": {}}), encoding="utf-8")
        with self.assertRaisesRegex(ReferenceLibraryError, "schema"):
            references.library_info(self.data_dir)

    def test_matcher_requires_library(self):
        with self.assertRaisesRegex(ReferenceLibraryError, "no reference library"):
            references.ReferenceMatcher(self.data_dir)


class MatcherTests(LibraryTestCase):
    def test_matches_upright_and_upside_down_crops(self):
        self.add_card(101, {1: 11, 2: 12}, name_en=None, name_ja="ジェーエー")
        self.add_card(102, {1: 21})
        self.add_card(103, {1: 31})
        matcher = references.ReferenceMatcher(self.data_dir)
        for upside_down in (False, True):
            with self.subTest(upside_down=upside_down):
                result = matcher.match(photo_crop(card_image(12), upside_down))
                self.assertIsNotNone(result)
                self.assertEqual(result["card"]["cid"], 101)
                self.assertEqual(set(result), {"card", "inliers", "inlier_ratio", "spread", "margin"})
                self.assertEqual(set(result["card"]), {"cid", "name_ko", "name_ja", "name_en", "image_path", "source_url"})
                self.assertEqual(result["card"]["name_ja"], "ジェーエー")
                self.assertIsNone(result["card"]["name_en"])
                self.assertTrue(Path(result["card"]["image_path"]).is_absolute())
                self.assertIn("_2_", Path(result["card"]["image_path"]).name)  # the matched artwork
                self.assertGreaterEqual(result["inliers"], references.MIN_INLIERS)
                self.assertGreaterEqual(result["margin"], references.MIN_MARGIN)

    def test_same_cid_artworks_do_not_compete(self):
        # Two near-identical artworks of one card would give an inlier margin of ~1 if scored as rivals.
        self.add_card(101, {1: 11, 2: 11})
        self.add_card(102, {1: 21})
        matcher = references.ReferenceMatcher(self.data_dir)
        result = matcher.match(photo_crop(card_image(11)))
        self.assertIsNotNone(result)
        self.assertEqual(result["card"]["cid"], 101)

    def test_same_art_on_different_cids_is_ambiguous(self):
        self.add_card(101, {1: 11})
        self.add_card(102, {1: 11})
        self.add_card(103, {1: 21})
        matcher = references.ReferenceMatcher(self.data_dir)
        crop = photo_crop(card_image(11))
        self.assertIsNone(matcher.match(crop))  # the shortlist vote already finds no unique card
        evidence = matcher.evidence(crop, shortlist=None)  # verifying every card, the margin rule decides
        self.assertGreaterEqual(min(evidence[101]["inliers"], evidence[102]["inliers"]), references.MIN_INLIERS)
        self.assertIsNone(references.decide(evidence))

    def test_same_art_shared_with_several_same_cid_artworks_is_ambiguous(self):
        self.add_card(101, {1: 11, 2: 11, 3: 11})
        self.add_card(102, {1: 11})
        self.add_card(103, {1: 21})
        matcher = references.ReferenceMatcher(self.data_dir)
        crop = photo_crop(card_image(11))
        self.assertIsNone(matcher.match(crop))
        self.assertIsNone(references.decide(matcher.evidence(crop, shortlist=None)))

    def test_partly_shared_illustration_rival_is_verified(self):
        # 102 shares the left 60% of 101's art: 101 alone gets distinctive votes (its right part),
        # yet 102 must still be verified so the margin is measured against that rival.
        self.add_card(101, {1: 11})
        self.add_card(102, {1: 11}, right_part_seed=99)
        self.add_card(103, {1: 21})
        matcher = references.ReferenceMatcher(self.data_dir)
        crop = photo_crop(card_image(11))
        evidence = matcher.evidence(crop)
        self.assertIn(102, evidence)
        self.assertGreaterEqual(evidence[102]["inliers"], references.MIN_INLIERS)
        result = matcher.match(crop)
        expected_margin = round(evidence[101]["inliers"] / evidence[102]["inliers"], 2)
        if result is None:
            self.assertLess(expected_margin, references.MIN_MARGIN)
        else:
            self.assertEqual((result["card"]["cid"], result["margin"]), (101, expected_margin))

    def test_unknown_blank_noise_and_text_only_are_unresolved(self):
        self.add_card(101, {1: 11})
        self.add_card(102, {1: 21})
        matcher = references.ReferenceMatcher(self.data_dir)
        crop = photo_crop(card_image(11))
        rgb = np.asarray(crop).copy()
        rgb[round(0.17 * rgb.shape[0]):round(0.70 * rgb.shape[0])] = 0
        noise = np.random.default_rng(0).integers(0, 256, (340, 235, 3), dtype=np.uint8)
        for name, image in {"unknown card": photo_crop(card_image(77)),
                            "blank": Image.new("RGB", (235, 340), (128, 128, 128)),
                            "noise": Image.fromarray(noise), "text only": Image.fromarray(rgb)}.items():
            with self.subTest(name):
                self.assertIsNone(matcher.match(image))
        self.assertIsNotNone(matcher.match(crop))  # the same pipeline does accept the real card

    def test_decide_requires_margin_over_other_cards(self):
        strong = {"inliers": 40, "inlier_ratio": 0.9, "spread": 0.5, "geometry_problem": None, "artwork": 0}
        self.assertEqual(references.decide({1: strong, 2: {**strong, "inliers": 10}})["margin"], 4.0)
        self.assertIsNone(references.decide({1: strong, 2: {**strong, "inliers": 25}}))
        self.assertIsNone(references.decide({1: {**strong, "geometry_problem": "projected art is mirrored"}}))
        self.assertIsNone(references.decide({1: {**strong, "inliers": references.MIN_INLIERS - 1}}))
        self.assertIsNone(references.decide({}))


# ---------------------------------------------------------------- enrollment with a fake Konami site

def detail_page(cid, name, artworks):
    script = "".join(f"$('#card_image_{n}').attr('src', '/yugiohdb/get_image.action?type=2&cid={cid}&ciid={ciid}&enc=Ab_c-1');\n"
                     for n, ciid in artworks)
    # A recommended other card, which must not be enrolled.
    script += "$('#card_image_0_1').attr('src', '/yugiohdb/get_image.action?type=2&cid=5&ciid=1&enc=Zz');\n"
    return f"<html><script>{script}</script><div id='cardname'><h1>{name}</h1></div></html>"


NO_DATA = "<html><div class='no_data'>no card</div></html>"


class FakeResponse:
    def __init__(self, url, body, content_type):
        self.url, self.status_code = url, 200
        self.headers = {"Content-Type": content_type}
        self.content = body if isinstance(body, bytes) else body.encode("utf-8")
        self.text = body if isinstance(body, str) else ""

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, pages):
        self.pages = pages  # url -> str (HTML), bytes (image) or Exception
        self.requested = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.requested.append(url)
        body = self.pages[url]
        if isinstance(body, Exception):
            raise body
        return FakeResponse(url, body, "application/octet-stream" if isinstance(body, bytes) else "text/html")


def image_url(cid, ciid):
    return f"{catalog.SITE}/yugiohdb/get_image.action?type=2&cid={cid}&ciid={ciid}&enc=Ab_c-1"


class EnrollmentTests(LibraryTestCase):
    def site(self, cid=22704, names=("Star", "スター", "별"), artworks=((1, 1), (2, 2)), seeds=None):
        pages = {}
        for locale, name in zip(("en", "ja", "ko"), names):
            pages[catalog.detail_url(cid, locale)] = NO_DATA if name is None else detail_page(cid, name, artworks)
        for _, ciid in artworks:
            pages[image_url(cid, ciid)] = png_bytes(card_image((seeds or {}).get(ciid, 50 + ciid)))
        return pages

    def test_artwork_union_uses_each_locale_context_and_downloads_each_ciid_once(self):
        pages = self.site(artworks=((1, 1), (2, 2), (3, 3)))
        for locale, ciids in (("en", (1,)), ("ja", (1, 2)), ("ko", (1, 2, 3))):
            pages[catalog.detail_url(22704, locale)] = detail_page(22704, locale, [(c, c) for c in ciids])

        class LocaleSession(FakeSession):
            locale = None

            def get(inner, url, **kwargs):
                for locale in ("en", "ja", "ko"):
                    if url == catalog.detail_url(22704, locale):
                        inner.locale = locale
                for ciid, locale in ((1, "en"), (2, "ja"), (3, "ko")):
                    if url == image_url(22704, ciid):
                        self.assertEqual(inner.locale, locale, "download must use the locale that exposed the artwork")
                return super().get(url, **kwargs)

        session = LocaleSession(pages)
        entry = references.enroll_card(session, 22704, self.data_dir)
        self.assertEqual([art["ciid"] for art in entry["artworks"]], [1, 2, 3])
        for ciid in (1, 2, 3):
            self.assertEqual(session.requested.count(image_url(22704, ciid)), 1)

    def test_duplicate_artwork_in_later_locale_still_validates_source(self):
        pages = self.site()
        pages[catalog.detail_url(22704, "ko")] = detail_page(22704, "별", ((1, 1),)).replace(
            "cid=22704&ciid=1", "cid=999&ciid=1")
        with self.assertRaisesRegex(ReferenceLibraryError, "unexpected artwork"):
            references.enroll_card(FakeSession(pages), 22704, self.data_dir)
        self.assertIsNone(references.library_info(self.data_dir))

    def test_late_locale_download_failure_preserves_previous_card(self):
        references.enroll_card(FakeSession(self.site(artworks=((1, 1),))), 22704, self.data_dir)
        before = references.library_info(self.data_dir)
        pages = self.site(artworks=((1, 1),), seeds={1: 99})
        pages[catalog.detail_url(22704, "ko")] = detail_page(22704, "별", ((1, 1), (2, 2)))
        pages[image_url(22704, 2)] = requests.ConnectionError("alternate artwork connection reset")
        with self.assertRaisesRegex(requests.ConnectionError, "alternate artwork"):
            references.enroll_card(FakeSession(pages), 22704, self.data_dir)
        self.assertEqual(references.library_info(self.data_dir), before)
        self.assertEqual(list(self.data_dir.glob(".staging-*")), [])

    def test_enrolls_names_and_every_artwork_but_not_recommendations(self):
        entry = references.enroll_card(FakeSession(self.site()), 22704, self.data_dir)
        self.assertEqual((entry["name_en"], entry["name_ja"], entry["name_ko"]), ("Star", "スター", "별"))
        self.assertEqual([a["ciid"] for a in entry["artworks"]], [1, 2])
        self.assertEqual(entry["source_url"], catalog.detail_url(22704, "en"))
        self.assertEqual(references.library_info(self.data_dir)["artwork_count"], 2)
        self.assertEqual(list(self.data_dir.glob(".staging-*")), [])

    def test_japanese_only_card_is_valid(self):
        entry = references.enroll_card(FakeSession(self.site(names=(None, "スター", None))), 22704, self.data_dir)
        self.assertEqual((entry["name_en"], entry["name_ja"], entry["name_ko"]), (None, "スター", None))
        self.assertEqual(entry["source_url"], catalog.detail_url(22704, "ja"))
        matcher = references.ReferenceMatcher(self.data_dir)
        self.assertEqual(matcher.cards[22704]["name_ja"], "スター")

    def test_card_missing_everywhere_fails(self):
        with self.assertRaisesRegex(ReferenceLibraryError, "no official detail page"):
            references.enroll_card(FakeSession(self.site(names=(None, None, None))), 22704, self.data_dir)
        self.assertIsNone(references.library_info(self.data_dir))

    def test_network_error_propagates_and_keeps_old_entry(self):
        references.enroll_card(FakeSession(self.site()), 22704, self.data_dir)
        before = references.library_info(self.data_dir)
        pages = self.site(seeds={1: 90, 2: 91})
        pages[image_url(22704, 2)] = requests.ConnectionError("connection reset")
        with self.assertRaises(requests.ConnectionError):
            references.enroll_card(FakeSession(pages), 22704, self.data_dir)
        self.assertEqual(references.library_info(self.data_dir), before)
        self.assertEqual(list(self.data_dir.glob(".staging-*")), [])

    def test_placeholder_image_fails_without_registering(self):
        pages = self.site()
        placeholder_sha = hashlib.sha256(pages[image_url(22704, 2)]).hexdigest()
        with mock.patch.object(catalog, "PLACEHOLDER_IMAGE_SHA256", placeholder_sha):
            with self.assertRaisesRegex(catalog.CatalogError, "Coming Soon"):
                references.enroll_card(FakeSession(pages), 22704, self.data_dir)
        self.assertIsNone(references.library_info(self.data_dir))

    def test_artwork_selector_must_carry_its_own_ciid(self):
        pages = self.site()
        pages[catalog.detail_url(22704, "en")] = pages[catalog.detail_url(22704, "en")].replace(
            "#card_image_2", "#card_image_3")
        with self.assertRaisesRegex(ReferenceLibraryError, "#card_image_3"):
            references.enroll_card(FakeSession(pages), 22704, self.data_dir)
        self.assertIsNone(references.library_info(self.data_dir))

    def test_artwork_of_another_cid_is_rejected(self):
        pages = self.site()
        pages[catalog.detail_url(22704, "en")] = pages[catalog.detail_url(22704, "en")].replace(
            "cid=22704&ciid=2", "cid=999&ciid=2")
        with self.assertRaisesRegex(ReferenceLibraryError, "unexpected artwork"):
            references.enroll_card(FakeSession(pages), 22704, self.data_dir)

    def test_re_enrollment_replaces_artworks_and_removes_old_files(self):
        references.enroll_card(FakeSession(self.site()), 22704, self.data_dir)
        old_files = sorted(p.name for p in (self.data_dir / "images").iterdir())
        entry = references.enroll_card(FakeSession(self.site(artworks=((1, 1),), seeds={1: 70})), 22704, self.data_dir)
        self.assertEqual([a["ciid"] for a in entry["artworks"]], [1])
        new_files = sorted(p.name for p in (self.data_dir / "images").iterdir())
        self.assertEqual(new_files, [Path(entry["artworks"][0]["image"]).name])
        self.assertNotEqual(old_files, new_files)
        self.assertEqual(references.library_info(self.data_dir)["artwork_count"], 1)

    def test_cid_from_url(self):
        self.assertEqual(references.cid_from_url(
            "https://www.db.yugioh-card.com/yugiohdb/card_search.action?ope=2&cid=22704&request_locale=ja"), 22704)
        for url in ("https://example.com/yugiohdb/card_search.action?ope=2&cid=1",
                    "https://www.db.yugioh-card.com/yugiohdb/card_search.action?ope=1&cid=1",
                    "https://www.db.yugioh-card.com/yugiohdb/card_search.action?ope=2&cid=abc",
                    "http://www.db.yugioh-card.com/yugiohdb/card_search.action?ope=2&cid=1"):
            with self.subTest(url):
                with self.assertRaises(ReferenceLibraryError):
                    references.cid_from_url(url)

    def test_cli_enrolls_cids_urls_and_catalog_once_each(self):
        catalog_dir = self.root / "catalog"
        image = self.image_file(card_image(1), "c.png")
        (catalog_dir / "images").mkdir(parents=True)
        (catalog_dir / "images" / "7_en.png").write_bytes(image.read_bytes())
        (catalog_dir / "cards.json").write_text(json.dumps({"seven": {
            "cid": 7, "name_en": "Seven", "name_ko": None, "name_ja": None, "image_path": "images/7_en.png",
            "source_url": catalog.detail_url(7, "en")}}), encoding="utf-8")
        argv = ["references.py", "--cid", "5", "7", "--url",
                "https://www.db.yugioh-card.com/yugiohdb/card_search.action?ope=2&cid=6",
                "--from-catalog", "--catalog-dir", str(catalog_dir), "--data-dir", str(self.data_dir)]
        enrolled = []

        def fake_enroll(session, cid, data_dir):
            enrolled.append(cid)
            return {"cid": cid, "name_en": "x", "name_ja": None, "name_ko": None, "artworks": [{"ciid": 1}]}

        with mock.patch.object(sys, "argv", argv), mock.patch.object(references, "enroll_card", fake_enroll), \
                mock.patch("builtins.print"):
            references.main()
        self.assertEqual(enrolled, [5, 7, 6])


ROOT = Path(__file__).resolve().parent.parent
# Konami cids of the user's sets (evaluation only; never passed to the matcher).
SET_TRUTH = {"set_1": [20282, 13844, 12707, 9270, 22139, 14209, 6530, 13825],
             "set_3": [22704, 22703, 22698, 22692, 22691, 22715, 19188, 14676, 20786, 22696]}


@unittest.skipUnless(
    (ROOT / "data" / "references" / "index.json").is_file() and (ROOT / "userDataset" / "set_3.jpg").is_file(),
    "needs the populated data/references library and userDataset photos")
class RealPhotoTests(unittest.TestCase):
    """Actual Recognizer crops of the user's sets against the shipped library (~20 s)."""

    def test_set_1_and_set_3_crops(self):
        import recognition
        if not recognition.models_ready():
            self.skipTest("DRAW2 models not downloaded")
        recognizer = recognition.Recognizer()
        matcher = references.ReferenceMatcher(ROOT / "data" / "references")
        for set_name, truth in SET_TRUTH.items():
            with self.subTest(set_name):
                crops = [r["crop"] for r in recognizer.recognize(Image.open(ROOT / "userDataset" / f"{set_name}.jpg"))]
                matched = [matcher.match(crop) for crop in crops]
                self.assertEqual(sorted(m["card"]["cid"] for m in matched if m is not None), sorted(truth))


if __name__ == "__main__":
    unittest.main()
