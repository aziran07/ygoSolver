"""Contract tests for deck-style export order, including strict metadata failures."""

import copy
import json
import random
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import deck_order
import exports
from export_order_fixtures import export_card, make_database, monster, spell_trap


class DeckOrderTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database = Path(directory.name) / "inventory.sqlite"

    def assert_order(self, records, expected):
        make_database(self.database, records)
        cards = [export_card(cid, name=f"逆順 {99999 - cid}") for cid, _, _ in records]
        random.Random(417).shuffle(cards)
        original = copy.deepcopy(cards)
        result = exports.sort_cards(cards, database_path=self.database)
        self.assertEqual([card["cid"] for card in result], expected)
        self.assertEqual(cards, original, "Export sorting must not reorder or edit input rows")

    def test_main_monsters_spells_traps_extra_and_pendulum_hybrids(self):
        records = [
            (10, monster("通常", level=1), 90010),
            (11, monster("ペンデュラム／通常", level=8), 90011),
            (12, monster("効果", level=4), 90012),
            (13, monster("ペンデュラム／効果", level=7), 90013),
            (14, monster("儀式／効果", level=1), 90014),
            (15, monster("儀式／ペンデュラム／効果", level=6), 90015),
            (20, spell_trap("魔法"), 90020),
            (21, spell_trap("魔法", "儀式"), 90021),
            (22, spell_trap("魔法", "速攻"), 90022),
            (23, spell_trap("魔法", "永続"), 90023),
            (24, spell_trap("魔法", "装備"), 90024),
            (25, spell_trap("魔法", "フィールド"), 90025),
            (30, spell_trap("罠"), 90030),
            (31, spell_trap("罠", "永続"), 90031),
            (32, spell_trap("罠", "カウンター"), 90032),
            (40, monster("融合／効果", level=4), 90040),
            (41, monster("融合／ペンデュラム／効果", level=8), 90041),
            (42, monster("シンクロ／効果", level=4), 90042),
            (43, monster("シンクロ／ペンデュラム／効果", level=8), 90043),
            (44, monster("エクシーズ／効果", level=4), 90044),
            (45, monster("エクシーズ／ペンデュラム／効果", level=8), 90045),
            (46, monster("リンク／効果", level=2), 90046),
            (47, monster("リンク／効果", level=4), 90047),
        ]
        self.assert_order(records, [11, 10, 13, 12, 15, 14, 20, 21, 22, 23, 24, 25,
                                    30, 31, 32, 41, 40, 43, 42, 45, 44, 47, 46])

    def test_level_then_attack_then_defense_descending_then_passcode_ascending(self):
        self.assert_order([
            (1, monster(level=7, attack=0, defense=0), 99),
            (2, monster(level=6, attack=3000, defense=0), 98),
            (3, monster(level=6, attack=2000, defense=2500), 97),
            (4, monster(level=6, attack=2000, defense=2000), 50),
            (5, monster(level=6, attack=2000, defense=2000), 10),
        ], [1, 2, 3, 5, 4])

    def test_unknown_stats_sort_below_zero_and_link_has_no_defense(self):
        self.assert_order([
            (1, monster(attack=0, defense=0), 50),
            (2, monster(attack="?", defense=5000), 10),
            (3, monster(attack=0, defense="?"), 1),
            (4, monster("リンク／効果", level=3, attack=0), 99),
            (5, monster("リンク／効果", level=2, attack=5000), 5),
            (6, monster("リンク／効果", level=3, attack=0), 10),
        ], [1, 3, 2, 6, 4, 5])

    def test_race_attribute_auxiliary_types_and_names_do_not_reorder_ties(self):
        tuner = monster("チューナー／効果")
        tuner.update(attribute="光属性", species="【 アンデット族／チューナー／効果 】")
        pendulum = monster("ペンデュラム／効果")
        pendulum["pendulum"] = "Pスケール 13"
        self.assert_order([(1, tuner, 20), (2, pendulum, 10),
                           (3, monster("リバース／効果"), 30),
                           (4, monster("ユニオン／効果"), 40)], [2, 1, 3, 4])

    def test_rarity_after_card_identity_and_quantities_are_unchanged(self):
        make_database(self.database, [(1, monster(), 20), (2, monster(), 10)])
        cards = exports.aggregate([export_card(1, rarity="SE@43"), export_card(1, rarity="SR"),
                                   export_card(2, rarity="UR"), export_card(1, quantity=2, rarity="N"),
                                   export_card(1, quantity=3, rarity="N"), export_card(1, rarity="SE")])
        actual = exports.sort_cards(cards, database_path=self.database)
        self.assertEqual([(c["cid"], c["rarity"], c["quantity"]) for c in actual],
                         [(2, "UR", 1), (1, "N", 5), (1, "SR", 1), (1, "SE", 1), (1, "SE@43", 1)])

    def test_missing_database_is_not_created_and_corrupt_database_is_explicit(self):
        with self.assertRaises(ValueError):
            deck_order.get_sort_keys([1], database_path=self.database)
        self.assertFalse(self.database.exists())
        self.database.write_bytes(b"this is not a SQLite file")
        with self.assertRaises(ValueError):
            deck_order.get_sort_keys([1], database_path=self.database)

    def test_missing_card_and_bad_metadata_do_not_receive_arbitrary_sort_keys(self):
        make_database(self.database, [(1, monster(), 100)])
        with self.assertRaisesRegex(ValueError, "999"):
            deck_order.get_sort_keys([1, 999], database_path=self.database)
        bad_infos = [
            {}, {"attribute": "不明"}, {**monster(), "species": "【 ドラゴン族／未知 】"},
            {**monster(), "level_rank": "レベル abc"},
            {**monster(), "atk": "攻撃力 1000x"},
            {**monster(), "def": "守備力 -"},
            {**monster(), "atk": "攻撃力 -100"},
            {**monster(), "level_rank": "ランク 4"},
            {**monster("リンク／効果"), "def": "守備力 100"},
            spell_trap("魔法", "カウンター"), spell_trap("罠", "速攻"),
        ]
        for info in bad_infos:
            with self.subTest(info=info):
                with closing(sqlite3.connect(self.database)) as connection, connection:
                    connection.execute("UPDATE cards SET list_info=?", (json.dumps({"ja": info}),))
                with self.assertRaisesRegex(ValueError, "1"):
                    deck_order.get_sort_keys([1], database_path=self.database)
        for raw in ('{broken', '[]', '{"ko": {}}', '{"ja": []}'):
            with self.subTest(raw=raw):
                with closing(sqlite3.connect(self.database)) as connection, connection:
                    connection.execute("UPDATE cards SET list_info=?", (raw,))
                with self.assertRaisesRegex(ValueError, "1"):
                    deck_order.get_sort_keys([1], database_path=self.database)

    def test_missing_passcodes_use_cid_only_after_type_and_stats_and_mapped_ties(self):
        self.assert_order([
            (1, monster(level=8), None),
            (2, monster(), None),
            (3, monster(), 90000),
            (4, monster(), 100),
            (5, monster(), None),
            (6, monster(level=1), 1),
            (7, spell_trap("魔法"), None),
            (8, spell_trap("罠"), None),
            (9, monster("融合／効果"), None),
        ], [1, 4, 3, 2, 5, 6, 7, 8, 9])

    def test_all_missing_passcodes_keep_card_identity_and_rarity_groups(self):
        make_database(self.database, [(10, monster(), None), (20, monster(), None)])
        cards = [export_card(20), export_card(10, rarity="SR"),
                 export_card(10, quantity=2), export_card(10, quantity=3)]
        for seed in range(3):
            with self.subTest(seed=seed):
                random.Random(seed).shuffle(cards)
                actual = exports.sort_cards(exports.aggregate(cards), database_path=self.database)
                self.assertEqual([(c["cid"], c["rarity"], c["quantity"]) for c in actual],
                                 [(10, "N", 5), (10, "SR", 1), (20, "N", 1)])

    def test_absent_passcode_does_not_hide_invalid_required_metadata(self):
        make_database(self.database, [(1, {**monster(), "atk": "broken"}, None)])
        with self.assertRaisesRegex(ValueError, "CID 1: .*broken"):
            deck_order.get_sort_keys([1], database_path=self.database)

    def test_invalid_present_passcode_is_explicit(self):
        make_database(self.database, [(1, monster(), 100)])
        for passcode in (0, -1, "broken", "", 1.5):
            with self.subTest(passcode=passcode):
                with closing(sqlite3.connect(self.database)) as connection, connection:
                    connection.execute("UPDATE cards SET ygoprodeck_id=?", (passcode,))
                with self.assertRaisesRegex(ValueError, "1"):
                    deck_order.get_sort_keys([1], database_path=self.database)

    def test_invalid_cids_are_not_silently_coerced(self):
        make_database(self.database, [(1, monster(), 100)])
        for cid in (True, 0, -1, "1", 1.0, None):
            with self.subTest(cid=cid), self.assertRaises(ValueError):
                deck_order.get_sort_keys([cid], database_path=self.database)
        for cids in ([1, True], [1, 1.0]):
            with self.subTest(cids=cids), self.assertRaises(ValueError):
                deck_order.get_sort_keys(cids, database_path=self.database)


if __name__ == "__main__":
    unittest.main()
