"""Codex live acceptance: actual models, official DB, and set_4 ground truth.

Run explicitly from the project root; missing official artwork is downloaded to
the application's candidate cache. This is not part of the offline unit suite.
"""

import csv
import json
import sys
from collections import Counter
from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import references  # noqa: E402


def main():
    base_before = references.library_info()
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()
    photo = ROOT / "userDataset/set_4.jpg"
    app.file_uploader[0].set_value([(photo.name, photo.read_bytes(), "image/jpeg")]).run()
    next(button for button in app.button if button.label == "카드 인식").click().run()
    assert not app.exception, app.exception
    assert not app.error, [error.value for error in app.error]
    rows = app.session_state["rows"]
    assert len(rows) == 9, len(rows)
    assert all(row["error"] is None for row in rows)
    with (ROOT / "userDataset/set_4.csv").open(encoding="utf-8-sig", newline="") as source:
        expected = {row["카드명"]: int(row["매수"]) for row in csv.DictReader(source)}
    actual = Counter()
    for row in rows:
        actual[row["card"]["name_ko"]] += row["quantity"]
    assert actual == expected, (actual, expected)
    assert (rows[2]["card"]["cid"], rows[2]["status"]) == (15627, "recognized")
    assert (rows[3]["card"]["cid"], rows[3]["status"]) == (15626, "review")
    for row, inliers in ((rows[2], 22), (rows[3], 8)):
        assert row["candidates"][0]["review"]["ciid"] == 2
        assert row["candidates"][0]["review"]["inliers"] == inliers
    assert references.library_info() == base_before, "candidate downloads changed the base library"
    assert all(button.proto.disabled for button in app.get("download_button"))
    report = [{"index": row["index"], "name_ko": row["card"]["name_ko"], "cid": row["card"]["cid"],
               "status": row["status"], "candidates": row["candidates"]} for row in rows]
    output = ROOT / "data/experiments/set4_candidate_review/app_acceptance.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"PASS: all 9 cards / 7 names and quantities match set_4.csv; row 3 verified, row 4 review. {output}")


if __name__ == "__main__":
    main()
