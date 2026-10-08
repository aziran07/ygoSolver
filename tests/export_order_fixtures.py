"""Small official-list-shaped records, independent of production sorting code."""

import json
import sqlite3
from contextlib import closing


def monster(types="効果", level=4, attack=1000, defense=1000):
    info = {"attribute": "闇属性", "species": f"【 ドラゴン族／{types} 】",
            "atk": f"攻撃力 {attack}", "def": f"守備力 {defense}"}
    if "リンク" in types.split("／"):
        info["link_markers"] = f"リンク {level}"
        info["def"] = "守備力 -"
    else:
        info["level_rank"] = f"{'ランク' if 'エクシーズ' in types.split('／') else 'レベル'} {level}"
    return info


def spell_trap(attribute, subtype=None):
    info = {"attribute": attribute}
    if subtype is not None:
        info["effect"] = subtype
    return info


def make_database(path, records):
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE cards (cid INTEGER PRIMARY KEY, list_info TEXT NOT NULL, "
                           "ygoprodeck_id INTEGER)")
        connection.executemany("INSERT INTO cards VALUES (?, ?, ?)",
                               [(cid, json.dumps({"ja": info}, ensure_ascii=False), passcode)
                                for cid, info, passcode in records])
    return path


def export_card(cid, name=None, quantity=1, rarity="N"):
    return {"cid": cid, "name": name or f"カード {cid}", "name_ko": f"카드 {cid}",
            "name_ja": f"カード {cid}", "name_en": f"Card {cid}",
            "quantity": quantity, "rarity": rarity}


# Existing UI tests isolate recognition/rarity/editing from local inventory I/O.
# Actual type/stat parsing and sorted downloads are tested with real SQLite fixtures.
APP_SORT_KEYS = {
    12950: (0, 1, -3, 0, -1800, 14558127, 12950),
    5050: (0, 1, -2, -500, -200, 23434538, 5050),
    7070: (0, 1, -1, 0, 0, 99999999, 7070),
    8002: (0, 1, -3, -1200, -900, 15175429, 8002),
    16537: (3, 3, -4, -3300, 0, 93672138, 16537),
    22691: (0, 1, -1, -300, -200, 11111111, 22691),
}


def app_sort_keys(cids, **kwargs):
    return {cid: APP_SORT_KEYS[cid] for cid in cids}
