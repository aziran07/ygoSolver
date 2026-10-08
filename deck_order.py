"""EDOPro-style deck order of official cards, read from the local official card DB.

get_sort_keys(cids) reads each card's Japanese official list metadata (cards.list_info["ja"]) and optional
passcode (cards.ygoprodeck_id) from data/official_cards/official_cards.sqlite and returns an ordering tuple per CID:

    (group, subtype, -level, -atk, -def, passcode is None, passcode, cid)

group: main-deck monsters < spells < traps < extra-deck monsters. Extra-deck monsters are Fusion, Synchro, Xyz and
Link, including their Pendulum hybrids; Ritual and other Pendulum monsters stay in the main deck.
subtype: main Normal < Effect < Ritual; extra Fusion < Synchro < Xyz < Link; spells Normal < Ritual < Quick-Play <
Continuous < Equip < Field; traps Normal < Continuous < Counter.
Monsters of one subtype sort by Level/Rank/Link rating, ATK, DEF (all descending), then passcode and CID ascending.
Some official cards have no passcode mapping (NULL ygoprodeck_id); only within an exact tie on the dimensions above do
they follow the mapped cards, ordered by CID. The "passcode is None" flag keeps None from being compared with ints.
An ATK/DEF of "?" sorts below 0; a Link monster's DEF "-" weighs the same for every Link monster. Spells and traps
use 0 for the stats. CID only separates different cards that share a passcode. Name, race, attribute, Pendulum
scale and Tuner/Spirit/Union/Flip status do not affect the order.

Missing or unparseable required data, or a present but invalid passcode, raises DeckOrderError; nothing is guessed and there is no alternative order.
"""

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import rarities

DATABASE_PATH = rarities.DATABASE_PATH

MAIN_MONSTER, SPELL, TRAP, EXTRA_MONSTER = range(4)
MAIN_SUBTYPES = ("通常", "効果", "儀式")  # Normal, Effect, Ritual
EXTRA_SUBTYPES = ("融合", "シンクロ", "エクシーズ", "リンク")  # Fusion, Synchro, Xyz, Link
# None is the Normal Spell/Trap, which has no "effect" field in the official list.
SPELL_SUBTYPES = (None, "儀式", "速攻", "永続", "装備", "フィールド")
TRAP_SUBTYPES = (None, "永続", "カウンター")
MONSTER_ATTRIBUTES = ("光属性", "闇属性", "地属性", "水属性", "炎属性", "風属性", "神属性")
# Monster type tokens after the race in the species field; only the subtype tokens affect the order.
SPECIES_TOKENS = {*MAIN_SUBTYPES, *EXTRA_SUBTYPES, "ペンデュラム", "チューナー", "スピリット", "ユニオン", "デュアル",
                  "トゥーン", "リバース", "特殊召喚"}
UNKNOWN_STAT = -1  # "?" ATK/DEF: below every printed value, including 0


class DeckOrderError(ValueError):
    pass


def get_sort_keys(cids, database_path=DATABASE_PATH):
    """Return {cid: ordering tuple} for every CID in cids."""
    cids = list(cids)
    # Checked before deduplication: a set would merge True or 1.0 into the int 1.
    for cid in cids:
        if type(cid) is not int or cid <= 0:
            raise DeckOrderError(f"CID must be a positive int, got {cid!r}")
    cids = set(cids)
    if not cids:
        return {}
    database_path = Path(database_path)
    if not database_path.is_file():
        raise DeckOrderError(f"카드 정렬용 공식 카드 DB가 없습니다: {database_path} (inventory.py 실행 필요)")
    try:
        with closing(sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            rows = {cid: (list_info, passcode) for cid, list_info, passcode in connection.execute(
                f"SELECT cid, list_info, ygoprodeck_id FROM cards WHERE cid IN ({','.join('?' * len(cids))})",
                sorted(cids))}
    except sqlite3.Error as error:
        raise DeckOrderError(f"카드 정렬용 공식 카드 DB를 읽을 수 없습니다: {database_path} ({error})") from error

    keys = {}
    for cid in sorted(cids):
        if cid not in rows:
            raise DeckOrderError(f"CID {cid}: 공식 카드 DB 스냅숏에 없어 정렬할 수 없습니다.")
        list_info, passcode = rows[cid]
        if passcode is not None and (type(passcode) is not int or passcode <= 0):
            raise DeckOrderError(f"CID {cid}: 공식 카드 DB의 카드 번호(passcode)가 잘못되어 정렬할 수 없습니다 "
                                 f"(ygoprodeck_id {passcode!r}).")
        try:
            japanese = json.loads(list_info)["ja"]
        except (json.JSONDecodeError, TypeError, KeyError) as error:
            raise DeckOrderError(f"CID {cid}: 일본어 공식 카드 정보가 없거나 손상되어 정렬할 수 없습니다 "
                                 f"({type(error).__name__}: {error}).") from error
        try:
            group, subtype, level, atk, defense = card_order(japanese)
        except (KeyError, TypeError, ValueError) as error:
            raise DeckOrderError(f"CID {cid}: 일본어 공식 카드 정보를 해석할 수 없어 정렬할 수 없습니다 "
                                 f"({type(error).__name__}: {error}).") from error
        keys[cid] = (group, subtype, -level, -atk, -defense, passcode is None, passcode, cid)
    return keys


def card_order(japanese):
    """(group, subtype index, level, atk, def) of one card's Japanese list fields; ValueError/KeyError if unknown."""
    attribute = japanese["attribute"]
    if attribute in ("魔法", "罠"):
        subtypes = SPELL_SUBTYPES if attribute == "魔法" else TRAP_SUBTYPES
        effect = japanese.get("effect")
        if effect not in subtypes:
            raise ValueError(f"unknown {attribute} subtype {effect!r}")
        return SPELL if attribute == "魔法" else TRAP, subtypes.index(effect), 0, 0, 0
    if attribute not in MONSTER_ATTRIBUTES:
        raise ValueError(f"unknown attribute {attribute!r}")

    match = re.fullmatch(r"【 ([^／]+)((?:／[^／]+)+) 】", japanese["species"])
    if match is None:
        raise ValueError(f"malformed species {japanese['species']!r}")
    tokens = match.group(2).split("／")[1:]
    unknown = [token for token in tokens if token not in SPECIES_TOKENS]
    if unknown or len(set(tokens)) != len(tokens):
        raise ValueError(f"unknown or repeated monster types {tokens!r}")

    extra = [token for token in tokens if token in EXTRA_SUBTYPES]
    if len(extra) > 1:
        raise ValueError(f"several extra-deck types {extra!r}")
    if extra:
        group, subtype = EXTRA_MONSTER, EXTRA_SUBTYPES.index(extra[0])
    elif "儀式" in tokens:
        group, subtype = MAIN_MONSTER, MAIN_SUBTYPES.index("儀式")
    elif ("通常" in tokens) != ("効果" in tokens):
        group, subtype = MAIN_MONSTER, MAIN_SUBTYPES.index("通常" if "通常" in tokens else "効果")
    else:
        raise ValueError(f"main-deck monster is neither exactly Normal nor Effect: {tokens!r}")

    is_link = extra == ["リンク"]
    if is_link:
        if "level_rank" in japanese:
            raise ValueError(f"Link monster with level_rank {japanese['level_rank']!r}")
        level = int(match_field(r"リンク (\d+)", japanese["link_markers"]))
    else:
        if "link_markers" in japanese:
            raise ValueError(f"non-Link monster with link_markers {japanese['link_markers']!r}")
        level_label = "ランク" if extra == ["エクシーズ"] else "レベル"
        level = int(match_field(level_label + r" (\d+)", japanese["level_rank"]))
    atk = parse_stat(japanese["atk"], "攻撃力", allow_dash=False)
    defense = parse_stat(japanese["def"], "守備力", allow_dash=is_link)
    return group, subtype, level, atk, defense


def parse_stat(text, label, allow_dash):
    """'攻撃力 1800' -> 1800, '?' -> UNKNOWN_STAT, '-' -> 0 (Link DEF only)."""
    value = match_field(label + r" (\d+|\?|-)", text)
    if value == "-":
        if not allow_dash:
            raise ValueError(f"{text!r} is only valid as a Link monster's DEF")
        return 0
    if allow_dash:
        raise ValueError(f"Link monster DEF must be '-', got {text!r}")
    return UNKNOWN_STAT if value == "?" else int(value)


def match_field(pattern, text):
    """The single group of pattern fully matching text; ValueError if text does not match."""
    match = re.fullmatch(pattern, text)
    if match is None:
        raise ValueError(f"{text!r} does not match {pattern!r}")
    return match.group(1)
