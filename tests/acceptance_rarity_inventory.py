"""Independent artifact audit; no application parser or rarity code is imported.

Run explicitly after full enrichment. --live compares selected card detail pages
with the saved product-derived print records, including variant rarity IDs.
"""

import argparse
import hashlib
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "data/official_cards"
EVIDENCE = ROOT / "data/experiments/rarity"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    path = DIRECTORY / "official_cards.sqlite"
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        meta = {key: json.loads(value) for key, value in connection.execute("SELECT * FROM rarity_meta")}
        assert meta["status"] == "complete", meta
        assert set(meta["locales"]) == {"ko", "ja"}
        rows = connection.execute("SELECT * FROM cards ORDER BY cid").fetchall()
        before = json.loads((EVIDENCE / "before_inventory.json").read_text(encoding="utf-8"))
        assert len(rows) == before["cards"] == 14342
        assert hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest() == before["cards_sha256"]
        manifest = json.loads((DIRECTORY / "manifest.json").read_text(encoding="utf-8"))
        assert hashlib.sha256(path.read_bytes()).hexdigest() == manifest["database"]["sha256"]
        expected_coverage = {(locale, cid) for cid, ko, ja in connection.execute("SELECT cid,name_ko,name_ja FROM cards")
                             for locale, name in (("ko", ko), ("ja", ja)) if name is not None}
        coverage = {(locale, cid): status for locale, cid, status in
                    connection.execute("SELECT locale,cid,status FROM rarity_coverage")}
        assert set(coverage) == expected_coverage
        prints = set(connection.execute("SELECT locale,pid,cid,rid,code FROM rarity_prints"))
        assert {p[0] for p in prints} == {"ko", "ja"}
        code_by_rid = {rid: json.loads(codes) for rid, codes in connection.execute("SELECT rid,codes FROM rarity_codes")}
        for locale, pid, cid, rid, code in prints:
            assert coverage[(locale, cid)] == "printed"
            assert code_by_rid[rid][locale] == code
        actual_printed = {(locale, cid) for locale, _, cid, _, _ in prints}
        assert actual_printed == {key for key, status in coverage.items() if status == "printed"}
        assert all(status in {"printed", "no_printing"} for status in coverage.values())
        snapshot = DIRECTORY / "rarity_snapshots" / meta["snapshot"]
        products = set(connection.execute("SELECT locale,pid FROM rarity_products"))
        checkpoint_prints = set()
        indexed = set()
        for locale in ("ko", "ja"):
            index = json.loads((snapshot / locale / "index.json").read_text(encoding="utf-8"))
            for product in index["products"]:
                pid = product["pid"]
                indexed.add((locale, pid))
                record = json.loads((snapshot / locale / "products" / f"{pid}.json").read_text(encoding="utf-8"))
                assert record["locale"] == locale and record["pid"] == pid
                assert len(record["rows"]) == record["total"]
                assert len({row["cid"] for row in record["rows"]}) == record["total"]
                for row in record["rows"]:
                    assert row["rarities"], (locale, pid, row["cid"])
                    for rarity in row["rarities"]:
                        checkpoint_prints.add((locale, pid, row["cid"], rarity["rid"], rarity["code"]))
        assert products == indexed
        assert prints == checkpoint_prints, (len(prints), len(checkpoint_prints), list(prints ^ checkpoint_prints)[:10])
        for locale, cid, url, sha in connection.execute(
                "SELECT locale,cid,evidence_url,evidence_sha256 FROM rarity_coverage WHERE status='no_printing'"):
            query = parse_qs(urlparse(url).query)
            assert query["cid"] == [str(cid)] and query["request_locale"] == [locale]
            assert re.fullmatch(r"[0-9a-f]{64}", sha)
            detail = json.loads((snapshot / locale / "details" / f"{cid}.json").read_text(encoding="utf-8"))
            assert detail["prints"] == [] and detail["html_sha256"] == sha
        live_checks = []
        if args.live:
            # Include known multi-finish cards and at least one observed special-color rarity.
            cids = {16537, 12950, 15627, 15626, 4007, 8002}
            for (cid,) in connection.execute("SELECT MIN(cid) FROM rarity_prints WHERE rid IN (43,50,54) GROUP BY rid"):
                cids.add(cid)
            with requests.Session() as session:
                for cid in sorted(cids):
                    for locale in ("ko", "ja"):
                        if (locale, cid) not in coverage:
                            continue
                        url = f"https://www.db.yugioh-card.com/yugiohdb/card_search.action?ope=2&cid={cid}&request_locale={locale}"
                        response = session.get(url, timeout=30)
                        response.raise_for_status()
                        soup = BeautifulSoup(response.text, "html.parser")
                        heading = soup.select_one("#cardname h1")
                        assert heading is not None, url
                        body = soup.select_one("#update_list .t_body")
                        assert body is not None, url
                        official = set()
                        for item in body.select(".t_row"):
                            pid = int(parse_qs(urlparse(item.select_one("input.link_value")["value"]).query)["pid"][0])
                            badges = item.select(".lr_icon")
                            assert badges, (url, str(item)[:200])
                            for icon in badges:
                                rid = [int(c[4:]) for c in icon["class"] if re.fullmatch(r"rid_\d+", c)]
                                assert len(rid) == 1
                                official.add((locale, pid, cid, rid[0], icon.select_one("p").get_text(strip=True)))
                        saved = {p for p in prints if p[0] == locale and p[2] == cid}
                        assert official == saved, (url, sorted(official ^ saved))
                        live_checks.append({"cid": cid, "locale": locale, "prints": len(saved), "url": url})
        report = {"cards_preserved": len(rows), "coverage": dict(Counter(l for l, _ in coverage)),
                  "products": len(products), "prints": len(prints), "rarities": len(code_by_rid),
                  "cids_with_rarity": len({p[2] for p in prints}), "live_checks": live_checks}
        (EVIDENCE / "independent_inventory_acceptance.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
