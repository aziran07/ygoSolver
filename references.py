"""Official-artwork reference library and pixel-based card matcher.

Library: data/references/index.json plus images/ holding every artwork Konami's
official card database lists for each enrolled card (card_image_N, i.e. all
ciids). Enroll cards with the CLI:

    .venv/Scripts/python references.py --cid 22704 22691
    .venv/Scripts/python references.py --url "https://www.db.yugioh-card.com/yugiohdb/card_search.action?ope=2&cid=22704"
    .venv/Scripts/python references.py --from-catalog          # every cid in data/catalog/cards.json

Matching (ReferenceMatcher.match): SIFT features of each artwork's illustration
window only (no name, text box or frame), Lowe ratio + mutual one-to-one
matches, RANSAC homography, then fixed acceptance thresholds on inlier count,
inlier ratio, inlier spread, plausible geometry, and the inlier margin over the
best *other* card. Artworks of the same cid never compete with each other.
A global FLANN index over all reference descriptors shortlists cards first
(see _shortlist), and only their artworks are geometrically verified. There is no probability: the
returned numbers are the visual evidence itself.

Coverage: only enrolled cards and the artworks Konami lists can match. Anything
else (unknown card, unlisted alternate art, glare, blank) is returned as None.
Enrollment collects the union of the artworks listed on every released locale's
(en/ja/ko) detail page; entries enrolled before that are not refreshed automatically.

Candidate review (CandidateReviewer): for a crop the classifier is unsure about
(needs_candidate_review: top score < 0.5 or top-two gap < 0.1), the top three model
candidates are resolved to exact official CIDs, all their artworks are enrolled into a
separate cache (data/candidate_references) and only those cards are compared with the
same matcher and thresholds. Full evidence accepts; a weaker but geometrically valid
lead only reorders for review; nothing qualifying keeps the model order.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np
from PIL import Image

import catalog

DATA_DIR = Path("data/references")
INDEX_NAME = "index.json"
SCHEMA_VERSION = 1
LOCALES = ("en", "ja", "ko")
CARD_FIELDS = {"cid", "name_en", "name_ja", "name_ko", "source_url", "artworks"}
ARTWORK_FIELDS = {"ciid", "image", "sha256"}
# Exactly '#card_image_<digits>': '#card_image_0_1' etc. are recommended *other* cards.
CARD_IMAGE_PATTERN = re.compile(r"\$\('#card_image_(\d+)'\)\.attr\('src',\s*'([^']+)'\)")
IMAGE_SRC_PATTERN = re.compile(r"^/yugiohdb/get_image\.action\?type=2&cid=(\d+)&ciid=(\d+)&enc=[\w-]+$")

# Illustration window as a fraction of the official card image (x0, y0, x1, y1):
# the intersection of the normal, pendulum and link art windows.
REFERENCE_ART_BOX = (0.12, 0.19, 0.88, 0.62)
REFERENCE_ART_WIDTH = 320
# Window on a detected crop: wider to tolerate detector misalignment, and
# vertically symmetric so an upside-down crop still holds the whole art (SIFT
# is rotation invariant). Text inside it cannot match: references hold none.
QUERY_ART_BOX = (0.04, 0.12, 0.96, 0.88)
QUERY_CARD_HEIGHT = 400

# Fixed acceptance thresholds (proven in data/experiments/reference; do not tune per photo).
RATIO_TEST = 0.75
RANSAC_REPROJECTION_PX = 4.0
MIN_INLIERS = 12
MIN_INLIER_RATIO = 0.25    # inliers / unique mutual matches
MIN_SPREAD = 0.15          # convex hull area of inliers / reference art area
MAX_SIDE_RATIO = 3.0       # opposite sides of the projected art quad
MIN_AREA_RATIO, MAX_AREA_RATIO = 0.25, 4.0
MIN_MARGIN = 2.0           # best card inliers / inliers of the best other card
SHORTLIST_CARDS = 8        # per vote; up to 2x this many cards are verified


class ReferenceLibraryError(Exception):
    pass


# ---------------------------------------------------------------- library files

def _index_path(data_dir):
    return Path(data_dir) / INDEX_NAME


def _validate_card(key, card, data_dir):
    problem = None
    if not isinstance(card, dict) or set(card) != CARD_FIELDS:
        problem = f"has fields {sorted(card) if isinstance(card, dict) else type(card).__name__}"
    elif type(card["cid"]) is not int or card["cid"] <= 0 or str(card["cid"]) != key:
        problem = f"has cid {card['cid']!r} that is not the positive integer {key!r}"
    elif any(card[f"name_{locale}"] is not None
             and (not isinstance(card[f"name_{locale}"], str) or not card[f"name_{locale}"].strip())
             for locale in LOCALES):
        problem = "has a name that is neither None nor a non-blank string"
    elif all(card[f"name_{locale}"] is None for locale in LOCALES):
        problem = "has no name in any locale"
    elif card["source_url"] not in [catalog.detail_url(card["cid"], locale) for locale in LOCALES]:
        problem = f"has unexpected source_url {card['source_url']!r}"
    elif not isinstance(card["artworks"], list) or not card["artworks"]:
        problem = "has no artworks"
    if problem is not None:
        raise ReferenceLibraryError(f"card {key} {problem}")

    ciids = set()
    for artwork in card["artworks"]:
        if not isinstance(artwork, dict) or set(artwork) != ARTWORK_FIELDS:
            raise ReferenceLibraryError(f"card {key} has a malformed artwork entry {artwork!r}")
        if type(artwork["ciid"]) is not int or artwork["ciid"] <= 0 or artwork["ciid"] in ciids:
            raise ReferenceLibraryError(f"card {key} has invalid or duplicate ciid {artwork['ciid']!r}")
        ciids.add(artwork["ciid"])
        image = artwork["image"]
        if not isinstance(image, str) or not re.fullmatch(rf"images/{key}_{artwork['ciid']}_[0-9a-f]{{12}}\.(png|jpg)", image):
            raise ReferenceLibraryError(f"card {key} ciid {artwork['ciid']} has invalid image path {image!r}")
        path = Path(data_dir) / image
        if not path.is_file():
            raise ReferenceLibraryError(f"card {key} ciid {artwork['ciid']} image is missing: {path}")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != artwork["sha256"]:
            raise ReferenceLibraryError(f"{path} does not match its recorded sha256")
        catalog.validate_image_bytes(data, path)


def load_index(data_dir=DATA_DIR):
    """Return the validated index dict, or None when the library was never initialized."""
    path = _index_path(data_dir)
    if not path.exists():
        return None
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ReferenceLibraryError(f"{path} is not valid JSON: {error}") from error
    if (not isinstance(index, dict) or set(index) != {"schema", "cards"}
            or type(index["schema"]) is not int or index["schema"] != SCHEMA_VERSION):
        raise ReferenceLibraryError(f"{path} is not a schema {SCHEMA_VERSION} reference index")
    if not isinstance(index["cards"], dict):
        raise ReferenceLibraryError(f"{path} 'cards' is not an object")
    for key, card in index["cards"].items():
        _validate_card(key, card, data_dir)
    return index


def _fingerprint(index):
    return hashlib.sha256(json.dumps(index, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def library_info(data_dir=DATA_DIR):
    """None when no index exists; otherwise counts and a fingerprint of the registered library.

    Any corrupt, mismatched or missing registered file raises ReferenceLibraryError.
    """
    index = load_index(data_dir)
    if index is None:
        return None
    return {"card_count": len(index["cards"]),
            "artwork_count": sum(len(card["artworks"]) for card in index["cards"].values()),
            "fingerprint": _fingerprint(index)}


def upsert_card(data_dir, card, artwork_files):
    """Register `card` (without artworks) with artwork_files {ciid: image file}, replacing any old entry.

    Images are copied in under content-addressed names first; the index is then
    replaced atomically, so a failure before that leaves the old entry intact.
    """
    data_dir = Path(data_dir)
    index = load_index(data_dir) or {"schema": SCHEMA_VERSION, "cards": {}}
    key = str(card["cid"])
    artworks = []
    for ciid, source in sorted(artwork_files.items()):
        data = Path(source).read_bytes()
        extension = {"PNG": ".png", "JPEG": ".jpg"}[catalog.validate_image_bytes(data, source)]
        sha256 = hashlib.sha256(data).hexdigest()
        image = f"images/{key}_{ciid}_{sha256[:12]}{extension}"
        if not (data_dir / image).is_file():
            catalog.write_atomic(data_dir / image, data)
        artworks.append({"ciid": ciid, "image": image, "sha256": sha256})
    entry = {**card, "artworks": artworks}
    _validate_card(key, entry, data_dir)

    old_images = {artwork["image"] for artwork in index["cards"].get(key, {}).get("artworks", [])}
    index["cards"][key] = entry
    index["cards"] = dict(sorted(index["cards"].items(), key=lambda item: int(item[0])))
    catalog.write_atomic(_index_path(data_dir), json.dumps(index, ensure_ascii=False, indent=1).encode("utf-8"))
    for image in old_images - {artwork["image"] for artwork in artworks}:
        (data_dir / image).unlink()
    return entry


# ---------------------------------------------------------------- enrollment (network)

def cid_from_url(url):
    """The cid of an official card detail URL; anything else raises ReferenceLibraryError."""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if (parsed.scheme != "https" or parsed.netloc != urlparse(catalog.SITE).netloc
            or parsed.path != "/yugiohdb/card_search.action" or query.get("ope") != ["2"]
            or len(query.get("cid", [])) != 1 or not query["cid"][0].isdigit() or int(query["cid"][0]) <= 0):
        raise ReferenceLibraryError(f"not an official card detail URL (…card_search.action?ope=2&cid=N): {url!r}")
    return int(query["cid"][0])


def artwork_sources(html, cid):
    """{ciid: image src} for every '#card_image_N' artwork of `cid` on a detail page."""
    sources = {}
    for selector_number, src in CARD_IMAGE_PATTERN.findall(html):
        match = IMAGE_SRC_PATTERN.match(src)
        # '#card_image_N' carries ciid=N on every verified page (including gaps such as 14, 16, 17).
        if (match is None or int(match.group(1)) != cid or int(match.group(2)) != int(selector_number)
                or int(match.group(2)) <= 0):
            raise ReferenceLibraryError(f"card {cid} detail page lists unexpected artwork "
                                        f"#card_image_{selector_number} src {src!r}")
        ciid = int(match.group(2))
        if ciid in sources:
            raise ReferenceLibraryError(f"card {cid} detail page lists ciid {ciid} twice")
        sources[ciid] = src
    if not sources:
        raise ReferenceLibraryError(f"card {cid} detail page lists no artwork")
    return sources


def enroll_card(session, cid, data_dir=DATA_DIR):
    """Fetch official names (None = Konami has no card in that locale) and every artwork, then register.

    Artworks are the union over every released locale's detail page: Konami lists some
    alternate artworks (ciids) only on the Japanese/Korean pages. Every locale's artwork
    listing is validated; each ciid is downloaded and validated once, from the first
    released locale listing it. Any failure leaves the old entry intact.
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=data_dir, prefix=".staging-")).resolve()
    if staging.parent != data_dir.resolve():
        raise ReferenceLibraryError(f"staging directory {staging} is not inside {data_dir.resolve()}")
    try:
        names, files, released = {}, {}, []
        for locale in LOCALES:
            url = catalog.detail_url(cid, locale)
            html = catalog.fetch_html(session, url)
            try:
                names[locale], _ = catalog.parse_detail_page(html)
            except catalog.CatalogError as error:
                raise catalog.CatalogError(f"{url}: {error}") from error
            if names[locale] is None:
                continue
            released.append(locale)
            # Images are fetched right after their detail page so the session renders that locale's card text.
            # Every locale's listing is validated; each ciid is downloaded once, by the first locale listing it.
            for ciid, src in artwork_sources(html, cid).items():
                if ciid not in files:
                    files[ciid] = catalog.download_image(session, src, url, staging / f"{cid}_{ciid}")
        if not released:
            raise ReferenceLibraryError(f"card {cid} has no official detail page in {', '.join(LOCALES)}")
        card = {"cid": cid, "name_en": names["en"], "name_ja": names["ja"], "name_ko": names["ko"],
                "source_url": catalog.detail_url(cid, released[0])}
        return upsert_card(data_dir, card, files)
    finally:
        shutil.rmtree(staging)


def catalog_cids(catalog_dir):
    cache_path = Path(catalog_dir) / "cards.json"
    cache = catalog.load_cache(cache_path)
    if not cache:
        raise ReferenceLibraryError(f"{cache_path} has no cards to enroll")
    return [catalog.validated_cached_card(cache_path, key, record, Path(catalog_dir))["cid"]
            for key, record in cache.items()]


# ---------------------------------------------------------------- matching

def _crop_fraction(image, box):
    height, width = image.shape[:2]
    x0, y0, x1, y1 = box
    return image[round(y0 * height):round(y1 * height), round(x0 * width):round(x1 * width)]


def _projected_quad_problem(homography, art_size, expected_area):
    """None when the reference art maps to a plausible convex, unmirrored quad; else why not."""
    width, height = art_size
    corners = np.float32([[0, 0], [width, 0], [width, height], [0, height]]).reshape(-1, 1, 2)
    quad = cv2.perspectiveTransform(corners, homography).reshape(4, 2).astype(np.float32)
    if not cv2.isContourConvex(quad):
        return "projected art is not convex"
    # Shoelace sum: positive for the reference corner order TL, TR, BR, BL in image coordinates.
    signed_area = sum(quad[i, 0] * quad[(i + 1) % 4, 1] - quad[(i + 1) % 4, 0] * quad[i, 1] for i in range(4))
    if signed_area <= 0:
        return "projected art is mirrored"
    sides = [np.linalg.norm(quad[(i + 1) % 4] - quad[i]) for i in range(4)]
    for a, b in ((sides[0], sides[2]), (sides[1], sides[3])):
        if max(a, b) > MAX_SIDE_RATIO * max(min(a, b), 1e-6):
            return "projected art has extreme perspective"
    area_ratio = cv2.contourArea(quad) / expected_area
    if not MIN_AREA_RATIO <= area_ratio <= MAX_AREA_RATIO:
        return f"projected art area is {area_ratio:.2f}x the expected area"
    return None


def decide(evidence, min_inliers=MIN_INLIERS):
    """{cid: best evidence over that card's artworks} -> accepted evidence dict, or None.

    min_inliers=0 asks only for valid geometry, ratio, spread and margin (CandidateReviewer's
    review-only lead); RANSAC's 4-point minimum still applies through valid geometry.
    """
    ranked = sorted(evidence.values(), key=lambda e: e["inliers"], reverse=True)
    if not ranked:
        return None
    best = ranked[0]
    runner_up_inliers = ranked[1]["inliers"] if len(ranked) > 1 else 0
    margin = best["inliers"] / max(runner_up_inliers, 1)
    if (best["geometry_problem"] is None and best["inliers"] >= min_inliers
            and best["inlier_ratio"] >= MIN_INLIER_RATIO and best["spread"] >= MIN_SPREAD
            and margin >= MIN_MARGIN):
        return {**best, "margin": round(margin, 2)}
    return None


class ReferenceMatcher:
    def __init__(self, data_dir=DATA_DIR, index=None):
        """index: an already validated index (or subset of one) of data_dir; loaded and validated when None."""
        data_dir = Path(data_dir)
        if index is None:
            index = load_index(data_dir)
        if index is None:
            raise ReferenceLibraryError(f"no reference library at {data_dir.resolve()}; enroll cards first")
        if not index["cards"]:
            raise ReferenceLibraryError(f"reference library {data_dir.resolve()} has no cards")
        self.fingerprint = _fingerprint(index)
        self.sift = cv2.SIFT_create()
        self.cards = {}
        self.artworks = []  # dicts: cid, image_path, art_size, points, descriptors
        for key, card in index["cards"].items():
            self.cards[card["cid"]] = {"cid": card["cid"], "name_ko": card["name_ko"], "name_ja": card["name_ja"],
                                       "name_en": card["name_en"], "source_url": card["source_url"]}
            for artwork in card["artworks"]:
                path = (data_dir / artwork["image"]).resolve()
                with Image.open(path) as image:
                    art = _crop_fraction(np.asarray(image.convert("RGB")), REFERENCE_ART_BOX)
                scale = REFERENCE_ART_WIDTH / art.shape[1]
                art = cv2.resize(art, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
                points, descriptors = self._features(art)
                if descriptors is None or len(descriptors) < MIN_INLIERS:
                    raise ReferenceLibraryError(f"{path} has only {0 if descriptors is None else len(descriptors)} "
                                                "SIFT features in its art window; it cannot be matched")
                self.artworks.append({"cid": card["cid"], "ciid": artwork["ciid"], "image_path": str(path),
                                      "art_size": art.shape[1::-1],
                                      "points": points, "descriptors": descriptors})

        # Coarse shortlist: one FLANN index over every reference descriptor.
        self.artwork_counts = {cid: sum(a["cid"] == cid for a in self.artworks) for cid in self.cards}
        self.descriptor_owner = np.concatenate(
            [np.full(len(a["descriptors"]), i) for i, a in enumerate(self.artworks)])
        self.flann = cv2.FlannBasedMatcher(dict(algorithm=1, trees=4), dict(checks=64))
        self.flann.add([np.concatenate([a["descriptors"] for a in self.artworks])])
        self.flann.train()
        self.brute_force = cv2.BFMatcher(cv2.NORM_L2)

    def _features(self, rgb):
        keypoints, descriptors = self.sift.detectAndCompute(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY), None)
        return np.float32([keypoint.pt for keypoint in keypoints]).reshape(-1, 2), descriptors

    def match(self, crop):
        """Accepted {card, inliers, inlier_ratio, spread, margin}, or None for insufficient/ambiguous evidence."""
        accepted = decide(self.evidence(crop))
        if accepted is None:
            return None
        artwork = self.artworks[accepted["artwork"]]
        card = {**self.cards[artwork["cid"]], "image_path": artwork["image_path"]}
        return {"card": card, "inliers": accepted["inliers"], "inlier_ratio": accepted["inlier_ratio"],
                "spread": accepted["spread"], "margin": accepted["margin"]}

    def evidence(self, crop, shortlist=SHORTLIST_CARDS):
        """{cid: strongest geometric evidence among that card's artworks} for the shortlisted cards.

        shortlist=None verifies every card (exhaustive, slow; used for evaluation).
        """
        rgb = np.asarray(crop.convert("RGB"))
        scale = QUERY_CARD_HEIGHT / rgb.shape[0]
        rgb = cv2.resize(rgb, (max(1, round(rgb.shape[1] * scale)), QUERY_CARD_HEIGHT), interpolation=cv2.INTER_CUBIC)
        window = _crop_fraction(rgb, QUERY_ART_BOX)
        points, descriptors = self._features(window)
        if descriptors is None or len(descriptors) < 2:
            return {}
        reference_fraction = (REFERENCE_ART_BOX[2] - REFERENCE_ART_BOX[0]) * (REFERENCE_ART_BOX[3] - REFERENCE_ART_BOX[1])
        expected_area = reference_fraction * rgb.shape[0] * rgb.shape[1]

        evidence = {}
        cids = list(self.cards) if shortlist is None else self._shortlist(descriptors, shortlist)
        for cid in cids:
            for number, artwork in enumerate(self.artworks):
                if artwork["cid"] != cid:
                    continue
                result = self._verify(artwork, points, descriptors, expected_area)
                result["artwork"] = number
                if cid not in evidence or result["inliers"] > evidence[cid]["inliers"]:
                    evidence[cid] = result
        return evidence

    def _shortlist(self, descriptors, count):
        """Cards to verify: the union of the top `count` cards by two votes over the whole library.

        - distinctive: a query descriptor votes for the card of its nearest
          neighbour only if that is clearly closer than the nearest descriptor of
          any *other* card (ratio test across cards, so artworks of one card do
          not cancel each other). Finds the true card.
        - shared: a query descriptor splits one vote among every card with a
          descriptor indistinguishable from its nearest neighbour. Finds cards
          that share an illustration with the true card, so the margin rule
          compares them instead of never seeing the rival. Divided by the card's
          artwork count, so cards with many artworks do not win it on noise.
        """
        distinctive, shared = {}, {}
        for neighbours in self.flann.knnMatch(descriptors, k=8):
            if not neighbours:
                continue
            owners = [self.artworks[self.descriptor_owner[n.trainIdx]]["cid"] for n in neighbours]
            nearest = neighbours[0].distance
            other = next((n for n, cid in zip(neighbours, owners) if cid != owners[0]), None)
            if other is not None and nearest < RATIO_TEST * other.distance:
                distinctive[owners[0]] = distinctive.get(owners[0], 0) + 1
            contenders = {cid for n, cid in zip(neighbours, owners) if RATIO_TEST * n.distance <= nearest}
            for cid in contenders:
                shared[cid] = shared.get(cid, 0) + 1 / len(contenders)
        shared = {cid: votes / self.artwork_counts[cid] for cid, votes in shared.items()}
        ranked = (sorted(distinctive, key=distinctive.get, reverse=True)[:count]
                  + sorted(shared, key=shared.get, reverse=True)[:count])
        return list(dict.fromkeys(ranked))

    def _verify(self, artwork, query_points, query_descriptors, expected_area):
        """Geometric evidence that `artwork` appears in the query window."""
        forward = self.brute_force.knnMatch(artwork["descriptors"], query_descriptors, k=2)
        backward = {m.queryIdx: m.trainIdx for m in self.brute_force.match(query_descriptors, artwork["descriptors"])}
        matches = [(first.queryIdx, first.trainIdx) for first, second in forward
                   if first.distance < RATIO_TEST * second.distance and backward.get(first.trainIdx) == first.queryIdx]
        result = {"inliers": 0, "inlier_ratio": 0.0, "spread": 0.0, "geometry_problem": None}
        if len(matches) < 4:
            result["geometry_problem"] = "fewer than 4 mutual matches"
            return result
        source = artwork["points"][[r for r, _ in matches]]
        target = query_points[[q for _, q in matches]]
        homography, mask = cv2.findHomography(source, target, cv2.RANSAC, RANSAC_REPROJECTION_PX)
        if homography is None:
            result["geometry_problem"] = "RANSAC found no homography"
            return result
        inliers = mask.ravel().astype(bool)
        width, height = artwork["art_size"]
        hull_area = cv2.contourArea(cv2.convexHull(source[inliers])) if inliers.sum() >= 3 else 0.0
        result.update(inliers=int(inliers.sum()), inlier_ratio=round(float(inliers.mean()), 3),
                      spread=round(hull_area / (width * height), 3),
                      geometry_problem=_projected_quad_problem(homography, artwork["art_size"], expected_area))
        return result


# ---------------------------------------------------------------- low-confidence candidate review

CANDIDATE_DIR = Path("data/candidate_references")
REVIEW_MAX_TOP_SCORE = 0.5   # review when the model's top score is below this ...
REVIEW_MIN_SCORE_GAP = 0.1   # ... or the top two scores are closer than this
REVIEW_CANDIDATE_COUNT = 3


class CandidateReviewError(Exception):
    pass


def needs_candidate_review(candidates):
    """True when the model's ranking is weak enough to compare its top candidates' official artworks."""
    if not candidates:
        return False
    top = candidates[0]["score"]
    second = candidates[1]["score"] if len(candidates) > 1 else 0.0
    return top < REVIEW_MAX_TOP_SCORE or top - second < REVIEW_MIN_SCORE_GAP


class CandidateReviewer:
    """Compares a crop with every official artwork (all locales) of its top model candidates only.

    Artworks live in their own library (data_dir), separate from the base reference library,
    so enrolling candidates never changes the base library's fingerprint. Cards already there
    are reused offline; a prepared matcher is kept per candidate set for this instance.
    """

    def __init__(self, data_dir=CANDIDATE_DIR):
        self.data_dir = Path(data_dir)
        self.index = load_index(self.data_dir) or {"schema": SCHEMA_VERSION, "cards": {}}
        self.matchers = {}  # tuple of cids -> ReferenceMatcher over only those cards

    def review(self, crop, candidates):
        """{"candidates": model candidates with a "review" entry, winner first; "accepted": bool}.

        The original card_id/name_en/name_ja/score are kept. A winner needs the matcher's
        valid geometry, ratio, spread and margin; it is "accepted" only with the automatic
        MIN_INLIERS as well, otherwise it merely leads for review. No qualifying winner:
        original order, not accepted. Failures raise CandidateReviewError naming stage and card.
        """
        reviewed = candidates[:REVIEW_CANDIDATE_COUNT]
        official = []
        for candidate in reviewed:
            try:
                official.append(catalog.resolve_card(candidate["name_en"]))
            except Exception as error:
                raise CandidateReviewError(f"resolving candidate {candidate['name_en']!r} in the official DB "
                                           f"failed: {type(error).__name__}: {error}") from error
        cids = list(dict.fromkeys(card["cid"] for card in official))
        self._enroll_missing(cids, official)
        for card in official:  # cached or just enrolled, the artworks must belong to this exact card
            cached_name = self.index["cards"][str(card["cid"])]["name_en"]
            if catalog.normalize_name(cached_name or "") != catalog.normalize_name(card["name_en"]):
                raise CandidateReviewError(f"CID {card['cid']} cached artwork name {cached_name!r} "
                                           f"differs from official DB name {card['name_en']!r}")
        cids_key = tuple(sorted(cids))
        if cids_key not in self.matchers:
            subset = {"schema": SCHEMA_VERSION, "cards": {str(cid): self.index["cards"][str(cid)] for cid in cids_key}}
            try:
                self.matchers[cids_key] = ReferenceMatcher(self.data_dir, subset)
            except Exception as error:
                raise CandidateReviewError(f"preparing official artworks of CIDs {list(cids_key)} failed: "
                                           f"{type(error).__name__}: {error}") from error
        matcher = self.matchers[cids_key]
        evidence = matcher.evidence(crop, shortlist=None)

        accepted = decide(evidence)
        leading = accepted or decide(evidence, min_inliers=0)
        winner_cid = None
        if leading is not None:
            winner_cid = matcher.artworks[leading["artwork"]]["cid"]

        enriched = []
        for candidate, card in zip(reviewed, official):
            enriched.append({**candidate, "review": self._review_entry(matcher, card, evidence.get(card["cid"]),
                                                                       leading if card["cid"] == winner_cid else None,
                                                                       accepted is not None)})
        enriched += candidates[REVIEW_CANDIDATE_COUNT:]
        if winner_cid is not None:
            winner = next(c for c in enriched if "review" in c and c["review"]["cid"] == winner_cid)
            enriched = [winner, *[c for c in enriched if c is not winner]]
        return {"candidates": enriched, "accepted": accepted is not None}

    def _enroll_missing(self, cids, official):
        missing = [cid for cid in cids if str(cid) not in self.index["cards"]]
        if not missing:
            return
        with catalog.new_session() as session:
            for cid in missing:
                name = next(card["name_en"] for card in official if card["cid"] == cid)
                try:
                    enroll_card(session, cid, self.data_dir)
                except Exception as error:
                    raise CandidateReviewError(f"downloading official artworks of candidate {name!r} (CID {cid}) "
                                               f"failed: {type(error).__name__}: {error}") from error
        self.index = load_index(self.data_dir)

    @staticmethod
    def _review_entry(matcher, card, evidence, winning, accepted):
        """Evidence for one candidate card; "card" shows its best-matching official artwork."""
        if evidence is None:  # the crop had too few features to compare at all
            evidence = {"inliers": 0, "inlier_ratio": 0.0, "spread": 0.0,
                        "geometry_problem": "crop has too few SIFT features", "artwork": None}
        artwork = (matcher.artworks[evidence["artwork"]] if evidence["artwork"] is not None
                   else next(a for a in matcher.artworks if a["cid"] == card["cid"]))
        outcome = "none"
        if winning is not None:
            outcome = "verified" if accepted else "leading"
        return {"cid": card["cid"], "ciid": artwork["ciid"], "card": {**card, "image_path": artwork["image_path"]},
                "inliers": evidence["inliers"], "inlier_ratio": evidence["inlier_ratio"],
                "spread": evidence["spread"], "geometry_problem": evidence["geometry_problem"],
                "margin": winning["margin"] if winning is not None else None, "outcome": outcome}


def main():
    parser = argparse.ArgumentParser(description="Enroll official Konami card artworks into the reference library.")
    parser.add_argument("--cid", type=int, nargs="+", default=[], help="Konami database card ids")
    parser.add_argument("--url", nargs="+", default=[], help="official card detail URLs (card_search.action?ope=2&cid=N)")
    parser.add_argument("--from-catalog", action="store_true", help="enroll every cid in <catalog-dir>/cards.json")
    parser.add_argument("--catalog-dir", type=Path, default=Path("data/catalog"))
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    args = parser.parse_args()

    cids = list(args.cid) + [cid_from_url(url) for url in args.url]
    if args.from_catalog:
        cids += catalog_cids(args.catalog_dir)
    if not cids:
        parser.error("give --cid, --url and/or --from-catalog")
    if any(cid <= 0 for cid in cids):
        parser.error("cids must be positive")
    cids = list(dict.fromkeys(cids))

    with catalog.new_session() as session:
        for number, cid in enumerate(cids, 1):
            card = enroll_card(session, cid, args.data_dir)
            name = card["name_en"] or card["name_ja"] or card["name_ko"]
            print(f"[{number}/{len(cids)}] enrolled {cid} {name}: ciid {[a['ciid'] for a in card['artworks']]}", flush=True)
    print(json.dumps(library_info(args.data_dir)))


if __name__ == "__main__":
    main()
