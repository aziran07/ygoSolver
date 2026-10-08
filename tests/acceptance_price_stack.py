"""Read-only live migration audit; --recreate also recreates this Compose stack.

Run on the deployment host from the repository root. Existing SQLite evidence
and PostgreSQL must contain the same first snapshot. No shop requests are made.
"""
import argparse
import hashlib
import json
import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def command(*arguments):
    result = subprocess.run(arguments, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"{arguments[:3]} failed (exit {result.returncode}): {result.stderr}")
    return result.stdout.strip()


def sql(query):
    return command("docker", "compose", "exec", "-T", "postgres", "psql", "-U", "ygosolver",
                   "-d", "ygosolver", "-v", "ON_ERROR_STOP=1", "-At", "-c", query)


def read_prices():
    return json.loads(command("docker", "compose", "run", "--rm", "-T", "--no-deps", "prices",
                              "get", "--card-number", "YAC1-JP002", "--locale", "ja"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recreate", action="store_true")
    options = parser.parse_args()
    config = json.loads(command("docker", "compose", "config", "--format", "json"))
    assert config["name"] == "ygosolver", "refusing another Compose project"
    for name in ("postgres", "redis"):
        service = config["services"][name]
        assert not service.get("ports"), f"{name} unexpectedly publishes a host port"
        assert service["restart"] == "unless-stopped"
        assert service["healthcheck"]
        assert any(mount["type"] == "volume" for mount in service["volumes"])
    # Do not print config: Compose resolves the password into its environment.
    del config

    capture = Path("data/prices/captures")
    metadata = json.loads((capture / "ja_list.json").read_text())
    assert hashlib.sha256((capture / "ja_list.bin").read_bytes()).hexdigest() == metadata["sha256"]
    fields = ("product_id", "name", "card_number", "locale", "rarity_label", "price_krw",
              "stock_status", "stock_evidence", "product_url")
    with closing(sqlite3.connect(f"file:{capture / 'tcgshop.sqlite'}?mode=ro", uri=True)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 1
        original_rows = connection.execute(f"SELECT {', '.join(fields)} FROM price_observations ORDER BY product_id").fetchall()
    pg_rows = json.loads(sql(f"SELECT json_agg(t) FROM (SELECT {', '.join(fields)} FROM price_observations ORDER BY product_id) t"))
    assert [tuple(row[field] for field in fields) for row in pg_rows] == original_rows
    assert len(pg_rows) == 56
    snapshots = json.loads(sql("SELECT json_agg(t) FROM (SELECT source_url, locale, scope, observed_at, status_code, sha256, bytes FROM price_snapshots) t"))
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    for left, right in [("source_url", "url"), ("status_code", "status_code"), ("sha256", "sha256"), ("bytes", "bytes")]:
        assert snapshot[left] == metadata[right]
    assert datetime.fromisoformat(snapshot["observed_at"]) == datetime.fromisoformat(metadata["observed_at"])
    assert snapshot["locale"] == "ja" and snapshot["scope"] == "single_list_page"

    first = read_prices()
    second = read_prices()
    assert second["cache_status"] == "hit"
    assert first["prices"] == second["prices"]
    assert {row["product_id"]: row["price_krw"] for row in first["prices"]} == {"134053": 28000, "134043": 6000}
    expected_expiry = datetime.fromisoformat(metadata["observed_at"]).timestamp() + 43200
    assert datetime.fromisoformat(first["expires_at"]).timestamp() == expected_expiry
    revision = sql("SELECT max(snapshot_id) FROM price_observations WHERE card_number='YAC1-JP002' AND locale='ja'")
    key = f"ygosolver:prices:v1:ja:YAC1-JP002:r{revision}"
    ttl = int(command("docker", "compose", "exec", "-T", "redis", "redis-cli", "PTTL", key))
    assert abs(ttl - (expected_expiry - datetime.now(timezone.utc).timestamp()) * 1000) < 2000
    report = {"products_matched": len(pg_rows), "sha256": metadata["sha256"],
              "observed_at": metadata["observed_at"], "expires_at": first["expires_at"],
              "cache_hit": True, "redis_ttl_ms": ttl, "recreated": False}
    if options.recreate:
        before = command("docker", "compose", "ps", "-q", "postgres", "redis").splitlines()
        command("docker", "compose", "up", "-d", "--force-recreate", "--wait", "postgres", "redis")
        after = command("docker", "compose", "ps", "-q", "postgres", "redis").splitlines()
        assert len(before) == len(after) == 2 and not set(before).intersection(after)
        assert sql("SELECT count(*) FROM price_observations") == "56"
        after_prices = read_prices()
        assert after_prices["prices"] == first["prices"]
        assert after_prices["expires_at"] == first["expires_at"]
        assert read_prices()["cache_status"] == "hit"
        report.update(recreated=True, before_containers=before, after_containers=after)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
