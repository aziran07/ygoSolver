# Third-party notices

This file lists third-party work used by ygoSolver. It does not set the license of ygoSolver itself.

## DRAW2 — models and recognition approach

- Source: https://github.com/HichTala/draw2 — Copyright (c) HichTala.
- Models: https://huggingface.co/HichTala/draw2 (model card declares `license: agpl-3.0`).
  Downloaded at runtime (not committed) from revision `1030c147c4d0c1c48a3467581ec317087d233f9d`:
  `onnx/ygo_yolo.onnx`, `onnx/vit_fp32.onnx`, `onnx/cardnames_onnx.json`.
- License: GNU Affero General Public License v3.0. Full text, copied verbatim from the DRAW2
  repository `LICENSE`: [LICENSES/DRAW2-AGPL-3.0.txt](LICENSES/DRAW2-AGPL-3.0.txt).
- `recognition.py` is a re-implementation that runs these models; its pre/post-processing constants follow
  DRAW2's `docs/scripts/pipeline.js` at commit `43b0a1c5fe5a987bb98d64ce7635c5581b72c9fb`.
  If you distribute this app or offer it to users over a network, review the AGPL-3.0 obligations
  (including making corresponding source available).
- The DRAW2 classifier is based on `google/vit-base-patch16-224-in21k` and trained on
  `HichTala/ygoprodeck-dataset`, as stated on the model card.

## TrackerYGO — crawler reference

- https://github.com/aziran07/TrackerYGO — `catalog.py` and `inventory.py` follow its approach for crawling the official
  card database (`src/crawling/scrape-cards.ts`, `scrape-cids.ts`). No code is copied.
- License: MIT — "Copyright (c) 2025 Aziran". Verified on 2026-10-07 from the repository's `LICENSE`
  file via the GitHub API (`gh api repos/aziran07/TrackerYGO/license`, SPDX `MIT`,
  https://github.com/aziran07/TrackerYGO/blob/main/LICENSE). The repository is not publicly visible,
  so unauthenticated web requests return 404.

## Official Yu-Gi-Oh! card database

- Card names, CIDs and reference images are looked up from https://www.db.yugioh-card.com and cached
  locally in `data/catalog`, `data/official_cards` and `data/references_missing` (not committed). Yu-Gi-Oh! names and card images © Studio Dice/SHUEISHA, TV TOKYO, KONAMI.

## Python dependencies

Installed from PyPI by `requirements.txt`, each under its own license:
Streamlit (Apache-2.0), ONNX Runtime (MIT), opencv-python-headless (Apache-2.0),
NumPy (BSD-3-Clause and bundled-component licenses), Pillow (MIT-CMU), Requests (Apache-2.0), Beautiful Soup 4 (MIT), openpyxl (MIT).
