"""Codex acceptance tests: shortlist review must add evidence, never invent confidence."""

import unittest
from unittest import mock

from PIL import Image

import catalog
import references
from test_references import LibraryTestCase, card_image, photo_crop


def candidate(cid, score):
    return {"card_id": str(100000 + cid), "name_en": f"Card {cid}", "name_ja": f"model {cid}", "score": score}


class CandidateReviewTests(LibraryTestCase):
    def setUp(self):
        super().setUp()
        self.candidates = [candidate(101, 0.0149), candidate(102, 0.0126), candidate(103, 0.0042)]
        self.official = {}
        for cid, seeds in ((101, {1: 11}), (102, {1: 21, 2: 22}), (103, {1: 31})):
            entry = self.add_card(cid, seeds, name_en=f"Card {cid}", name_ko=f"공식 {cid}")
            self.official[f"Card {cid}"] = {k: v for k, v in entry.items() if k != "artworks"}
        patch = mock.patch.object(catalog, "resolve_card", side_effect=lambda name: self.official[name])
        self.resolve = patch.start()
        self.addCleanup(patch.stop)

    def test_trigger_uses_low_score_or_small_gap(self):
        for scores, expected in (([], False), ([0.49, 0.1], True), ([0.8, 0.75], True),
                                 ([0.8, 0.2], False), ([0.9], False), ([0.1], True)):
            with self.subTest(scores=scores):
                self.assertEqual(references.needs_candidate_review([candidate(i, s) for i, s in enumerate(scores)]),
                                 expected)

    def test_alternate_artwork_can_win_without_changing_model_scores(self):
        originals = [dict(c) for c in self.candidates]
        result = references.CandidateReviewer(self.data_dir).review(photo_crop(card_image(22)), self.candidates)
        self.assertTrue(result["accepted"])
        winner = result["candidates"][0]
        self.assertEqual(winner["card_id"], "100102")
        self.assertEqual((winner["review"]["cid"], winner["review"]["ciid"]), (102, 2))
        self.assertEqual(winner["review"]["outcome"], "verified")
        self.assertGreaterEqual(winner["review"]["inliers"], 12)
        self.assertIn("102_2_", winner["review"]["card"]["image_path"])
        self.assertEqual([{k: c[k] for k in originals[0]} for c in result["candidates"]],
                         [originals[1], originals[0], originals[2]])
        self.assertEqual(self.candidates, originals, "review must not mutate the classifier output")

    def test_only_requested_top_three_cards_compete(self):
        self.add_card(999, {1: 99}, name_en="Outside shortlist")
        candidates = self.candidates + [candidate(999, 0.001)]
        reviewer = references.CandidateReviewer(self.data_dir)
        result = reviewer.review(photo_crop(card_image(99)), candidates)
        self.assertFalse(result["accepted"])
        self.assertEqual([c["card_id"] for c in result["candidates"]], [c["card_id"] for c in candidates])
        self.assertEqual(result["candidates"][3], candidates[3])
        self.assertEqual(self.resolve.call_args_list, [mock.call(c["name_en"]) for c in self.candidates])

    def test_blank_crop_has_no_winner_and_retains_order(self):
        result = references.CandidateReviewer(self.data_dir).review(Image.new("RGB", (240, 340)), self.candidates)
        self.assertFalse(result["accepted"])
        self.assertEqual([c["card_id"] for c in result["candidates"]], [c["card_id"] for c in self.candidates])
        self.assertTrue(all(c["review"]["outcome"] == "none" for c in result["candidates"]))

    def test_disk_artworks_and_features_are_reused_without_download(self):
        with mock.patch.object(catalog, "new_session", side_effect=AssertionError("unexpected network")), \
                mock.patch.object(references, "ReferenceMatcher", wraps=references.ReferenceMatcher) as matcher:
            reviewer = references.CandidateReviewer(self.data_dir)
            crop = photo_crop(card_image(22))
            self.assertTrue(reviewer.review(crop, self.candidates)["accepted"])
            self.assertTrue(reviewer.review(crop, list(reversed(self.candidates)))["accepted"])
            self.assertEqual(matcher.call_count, 1)
            self.assertTrue(references.CandidateReviewer(self.data_dir).review(crop, self.candidates)["accepted"])

    def evidence(self, reviewer, winner, rival=4, **overrides):
        # Fixed user-facing gates; these values are independent of the code's constants.
        def fake_evidence(matcher, crop, shortlist=None):
            output = {}
            for cid in (101, 102, 103):
                index = next(i for i, art in enumerate(matcher.artworks) if art["cid"] == cid)
                output[cid] = {"artwork": index, "inliers": winner if cid == 102 else rival,
                               "inlier_ratio": 0.4, "spread": 0.25, "geometry_problem": None}
            output[102].update(overrides)
            return output
        with mock.patch.object(references.ReferenceMatcher, "evidence", fake_evidence):
            return reviewer.review(photo_crop(card_image(22)), self.candidates)

    def test_eight_inliers_can_reorder_but_cannot_auto_confirm(self):
        result = self.evidence(references.CandidateReviewer(self.data_dir), 8)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["candidates"][0]["card_id"], "100102")
        self.assertEqual(result["candidates"][0]["review"]["outcome"], "leading")

    def test_ties_bad_geometry_and_concentrated_features_cannot_reorder(self):
        reviewer = references.CandidateReviewer(self.data_dir)
        cases = [(8, 8, {}), (20, 4, {"geometry_problem": "mirrored"}),
                 (20, 4, {"spread": 0.1}), (20, 4, {"inlier_ratio": 0.2})]
        for winner, rival, overrides in cases:
            with self.subTest(overrides=overrides, rival=rival):
                result = self.evidence(reviewer, winner, rival, **overrides)
                self.assertFalse(result["accepted"])
                self.assertEqual([c["card_id"] for c in result["candidates"]], [c["card_id"] for c in self.candidates])

    def test_official_lookup_failure_does_not_return_model_only_success(self):
        self.resolve.side_effect = catalog.CatalogError("official search HTTP 503")
        with self.assertRaisesRegex(references.CandidateReviewError, "Card 101.*HTTP 503"):
            references.CandidateReviewer(self.data_dir).review(photo_crop(card_image(22)), self.candidates)

    def test_missing_artwork_download_failure_identifies_candidate(self):
        self.official["Card 104"] = {**self.official["Card 103"], "cid": 104, "name_en": "Card 104"}
        with mock.patch.object(catalog, "new_session"), \
                mock.patch.object(references, "enroll_card", side_effect=OSError("image unavailable")):
            with self.assertRaisesRegex(references.CandidateReviewError, "Card 104.*104.*image unavailable"):
                references.CandidateReviewer(self.data_dir).review(photo_crop(card_image(22)), [candidate(104, .1)])

    def test_cached_artwork_identity_mismatch_is_not_reused(self):
        self.add_card(102, {1: 22}, name_en="Different official name")
        with self.assertRaisesRegex(references.CandidateReviewError, "102.*name"):
            references.CandidateReviewer(self.data_dir).review(photo_crop(card_image(22)), self.candidates)

    def test_corrupt_cached_image_is_an_explicit_failure(self):
        entry = references.load_index(self.data_dir)["cards"]["102"]
        (self.data_dir / entry["artworks"][0]["image"]).write_bytes(b"bad image")
        with self.assertRaises(ReferenceLibraryError):
            references.CandidateReviewer(self.data_dir)


ReferenceLibraryError = references.ReferenceLibraryError


if __name__ == "__main__":
    unittest.main()
