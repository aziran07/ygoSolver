"""Read-only deployed price lookup and real AppTest against the existing capture."""
import csv
import io
import json
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import card_prices
import catalog
import exports
from streamlit.testing.v1 import AppTest

prints = card_prices.fetch_official_prints(5631, "ja")
price = card_prices.get_card_price(5631, "N", "ja", prints, os.environ["DATABASE_URL"], os.environ["REDIS_URL"])
assert price["status"] == "ok", price
assert price["unit_price_krw"] == 240 and price["card_number"] == "15AY-JPB22"
assert price["product_id"] == "39577"
assert price["observed_at"] == datetime(2026, 10, 8, 7, 19, 37, 504356, tzinfo=timezone.utc)
card = catalog.resolve_card("Diffusion Wave-Motion")
assert card["cid"] == 5631
row = {"key": "live_price", "source": "manual", "image_name": None, "index": None, "crop": None,
       "candidates": [], "status": "manual", "choice": None, "card": card, "error": None,
       "quantity": 3, "name_override": ""}
app = AppTest.from_file(str(Path("app.py").resolve()), default_timeout=90)
app.session_state["rows"] = [row]
app.run()
assert not app.exception, app.exception
assert any("240" in label for label in app.selectbox(key="rarity_live_price").options)
table = app.dataframe[0].value
assert list(table["단가(원)"]) == [240]
assert list(table["합계(원)"]) == [720]
app.checkbox(key="confirmed").check().run()
assert not app.exception, app.exception
assert all(not b.proto.disabled for b in app.get("download_button"))
entry = {**card, "name": card["name_ko"], "rarity": "N", "quantity": 3, "price": price}
data = exports.to_csv_bytes([entry], include_price=True)
parsed = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
assert parsed[0]["단가(원)"] == "240" and parsed[0]["합계(원)"] == "720"
print(json.dumps({"cid": 5631, "unit_price_krw": 240, "quantity": 3, "total_price_krw": 720,
                  "observed_at": price["observed_at"].isoformat(), "expires_at": price["expires_at"].isoformat(),
                  "card_number": price["card_number"], "product_url": price["product_url"],
                  "app_exceptions": 0, "confirmed_downloads_enabled": True,
                  "csv_verified": True}, ensure_ascii=False, indent=2))
