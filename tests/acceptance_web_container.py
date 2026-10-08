"""Run inside the deployed web image with its normal data mounts.

docker compose run --rm --no-deps -T -v "$PWD/tests:/app/tests:ro" \
    --entrypoint python web tests/acceptance_web_container.py
This checks real runtime data and permissions, not HTTP/browser behavior.
"""
import errno
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import catalog
import rarities
import recognition
import references
from streamlit.testing.v1 import AppTest


def main():
    assert os.getuid() == 1000, f"unexpected runtime UID: {os.getuid()}"
    assert not Path(".env").exists()
    assert not Path(".git").exists()
    assert not Path("userDataset").exists()
    assert "POSTGRES_PASSWORD" not in os.environ
    assert urlsplit(os.environ["DATABASE_URL"]).hostname == "postgres"
    assert urlsplit(os.environ["REDIS_URL"]).hostname == "redis"

    for directory in ("data/models", "data/references", "data/official_cards"):
        try:
            with tempfile.TemporaryFile(dir=directory):
                raise AssertionError(f"{directory} is unexpectedly writable")
        except OSError as error:
            assert error.errno == errno.EROFS, (directory, error)
    for directory in ("data/catalog", "data/candidate_references"):
        with tempfile.TemporaryFile(dir=directory) as target:
            target.write(b"runtime cache permission check")
            target.flush()

    # Constructor checks pinned model hashes and actually opens ONNX sessions.
    recognizer = recognition.Recognizer()
    assert recognizer is not None
    library = references.library_info()
    assert library is not None and library["card_count"] > 0 and library["artwork_count"] > 0
    references.ReferenceMatcher()
    card = catalog.resolve_card("Blue-Eyes White Dragon")
    assert card["cid"] == 4007 and card["name_ko"] == "푸른 눈의 백룡"
    assert rarities.get_rarities(4007)

    app = AppTest.from_file(str(Path("app.py").resolve()), default_timeout=30).run()
    assert not app.exception, app.exception
    assert not app.error, [item.value for item in app.error]
    assert len(app.file_uploader) == 1
    recognize = next(button for button in app.button if button.label == "카드 인식")
    assert recognize.disabled, "recognition should be disabled before upload"
    assert not any(button.label.startswith("모델 다운로드") for button in app.button)
    print(json.dumps({"uid": os.getuid(), "models_loaded": True,
                      "reference_cards": library["card_count"],
                      "reference_artworks": library["artwork_count"],
                      "official_card_cid": card["cid"], "initial_app_errors": 0,
                      "readonly_and_writable_mounts_verified": True}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
