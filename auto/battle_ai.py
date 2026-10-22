"""Pick the best move (and target, in double battles) for FireRed, from type matchups and base stats.

Not a full damage calculator: we don't know the exact stats, only species, levels and moves.
The score is "expected damage" ~ power x type x STAB x accuracy x (attack / defence) x level ratio.
"""
import difflib
import json
import unicodedata
from pathlib import Path

DATA = json.loads((Path(__file__).parent / "data" / "gen3.json").read_text(encoding="utf-8"))
MOVES, SPECIES, EFF = DATA["moves"], DATA["species"], DATA["eff"]

TYPE_KO = {"normal": "노말", "fire": "불꽃", "water": "물", "electric": "전기", "grass": "풀", "ice": "얼음",
           "fighting": "격투", "poison": "독", "ground": "땅", "flying": "비행", "psychic": "에스퍼", "bug": "벌레",
           "rock": "바위", "ghost": "고스트", "dragon": "드래곤", "dark": "악", "steel": "강철"}
MULTI3 = {"double-slap", "comet-punch", "fury-attack", "pin-missile", "spike-cannon", "barrage", "fury-swipes",
          "bone-rush", "arm-thrust", "bullet-seed", "icicle-spear", "rock-blast"}
MULTI2 = {"double-kick", "bonemerang", "twineedle"}
AVOID = {"self-destruct", "explosion", "dream-eater", "focus-punch", "counter", "mirror-coat", "bide", "fissure",
         "guillotine", "horn-drill", "sheer-cold", "spit-up", "snore", "fake-out", "memento", "perish-song"}
CHARGE = {"solar-beam": 0.5, "sky-attack": 0.5, "razor-wind": 0.5, "skull-bash": 0.5, "hyper-beam": 0.55,
          "blast-burn": 0.55, "hydro-cannon": 0.55, "frenzy-plant": 0.55, "dig": 0.75, "fly": 0.75, "dive": 0.75,
          "bounce": 0.75}
VARIABLE = {"return": 70, "frustration": 40, "low-kick": 60, "magnitude": 70, "present": 40, "hidden-power": 50,
            "flail": 40, "reversal": 40, "eruption": 100, "water-spout": 100, "super-fang": 50, "endeavor": 0,
            "psywave": 40}
FIXED = {"dragon-rage": 40, "sonic-boom": 20}
LEVEL_DMG = {"seismic-toss", "night-shade"}


def norm(s):
    return unicodedata.normalize("NFKC", s or "").replace(" ", "").replace("　", "")


def _fuzzy(name, table, cutoff=0.6):
    n = norm(name)
    if n in table:
        return n
    m = difflib.get_close_matches(n, table.keys(), n=1, cutoff=cutoff)
    return m[0] if m else None


def find_move(name):
    k = _fuzzy(name, MOVES)
    return (k, MOVES[k]) if k else (None, None)


def find_species(name):
    k = _fuzzy(name, SPECIES, 0.5)
    return (k, SPECIES[k]) if k else (None, None)


def _kata(s):
    return "".join(c for c in norm(s) if "ァ" <= c <= "ヺ" or c in "ー♂♀")


_BY_LEN = sorted(SPECIES, key=len, reverse=True)


def clean_species(raw):
    """OCR of an HP box ('ワンリキーgL3日', 'ナッシーさ', 'ESP') -> species name or None.
    Keeps the katakana, then: longest species name it starts with, else a close match."""
    k = _kata(raw)
    if len(k) < 2:
        return None
    for name in _BY_LEN:
        if k.startswith(name) and len(name) >= 2:
            return name
    m = difflib.get_close_matches(k, SPECIES.keys(), n=1, cutoff=0.75)
    return m[0] if m else None


def effectiveness(mtype, def_types):
    e = 1.0
    for t in def_types:
        e *= EFF.get(mtype, {}).get(t, 1.0)
    return e


def eff_label(e):
    if e == 0:
        return "효과 없음 ×0"
    if e < 1:
        return f"별로 ×{e:g}"
    if e > 1:
        return f"효과 굉장 ×{e:g}"
    return "보통 ×1"


def damage_score(move, atk, dfn, atk_lv=None, def_lv=None):
    """Expected damage index of one hit on one target. atk/dfn: species dicts."""
    m = move
    if m["cat"] == "status" and m["en"] not in LEVEL_DMG | set(FIXED) | set(VARIABLE):
        return 0.0, "변화기"
    if m["en"] in AVOID:
        return 0.0, "안 씀"
    e = effectiveness(m["type"], dfn["types"])
    if e == 0:
        return 0.0, eff_label(0)
    acc = (m["acc"] or 100) / 100
    if m["en"] in LEVEL_DMG or m["en"] in FIXED:
        # fixed damage: compare to a typical hit of power 60 at this level
        hp = FIXED.get(m["en"], atk_lv or 40)
        return hp * 1.2 * acc, f"고정 {hp}"
    power = m["power"] or VARIABLE.get(m["en"], 0)
    if not power:
        return 0.0, "위력 없음"
    if m["en"] in MULTI3:
        power *= 3
    elif m["en"] in MULTI2:
        power *= 2
    power *= CHARGE.get(m["en"], 1.0)
    stab = 1.5 if m["type"] in atk["types"] else 1.0
    a = atk["base"]["atk" if m["cat"] == "phys" else "spa"]
    d = dfn["base"]["def" if m["cat"] == "phys" else "spd"]
    ratio = (a + 50) / (d + 50)
    lvr = (atk_lv / def_lv) if atk_lv and def_lv else 1.0
    note = eff_label(e) + (" · 자속" if stab > 1 else "")
    return power * e * stab * acc * ratio * lvr, note


TYPE_JA = {"ノーマル": "normal", "ほのお": "fire", "みず": "water", "でんき": "electric", "くさ": "grass", "こおり": "ice",
           "かくとう": "fighting", "どく": "poison", "じめん": "ground", "ひこう": "flying", "エスパー": "psychic",
           "むし": "bug", "いわ": "rock", "ゴースト": "ghost", "ドラゴン": "dragon", "あく": "dark", "はがね": "steel"}


def type_from_ja(text):
    """'でんき' (OCR of the move type box, maybe with junk) -> 'electric' or None."""
    t = norm(text)
    for ja, en in TYPE_JA.items():
        if ja in t:
            return en
    m = difflib.get_close_matches(t[:5], TYPE_JA.keys(), n=1, cutoff=0.6)
    return TYPE_JA[m[0]] if m else None


def choose(my_name, my_lv, moves, foes, partner=None, doubles=None, exclude=()):
    """moves: up to 4 move names in menu order (top-left, top-right, bottom-left, bottom-right).
    foes: list of (name, lv) on the field (1 or 2). partner: (name, lv) of my other Pokemon in doubles.
    Returns dict(slot, target, move, score, why, table)."""
    _, me = find_species(my_name)
    if me is None:
        return None
    foes = [(n, lv, find_species(n)[1]) for n, lv in foes]
    foes = [(n, lv, s) for n, lv, s in foes if s]
    if not foes:
        return None
    psp = find_species(partner[0])[1] if partner else None
    doubles = (len(foes) > 1 or psp is not None) if doubles is None else doubles
    table = []
    for slot, mname in enumerate(moves):
        if slot in exclude:                           # e.g. no PP left
            continue
        key, mv = find_move(mname) if mname else (None, None)
        if mv is None:
            continue
        spread = mv["target"] in (9, 11) or mv["en"] in ("surf", "earthquake", "magnitude", "explosion",
                                                       "self-destruct", "rock-slide", "razor-leaf", "blizzard",
                                                       "heat-wave", "icy-wind", "swift", "growl")
        if doubles and spread:
            hits_partner = mv["target"] == 9 and psp is not None   # earthquake & co. also hit my partner
            n_targets = len(foes) + (1 if hits_partner else 0)
            share = 0.5 if n_targets > 1 else 1.0                  # Gen 3: half damage when it hits several
            total, notes = 0.0, []
            for n, lv, s in foes:
                sc, note = damage_score(mv, me, s, my_lv, lv)
                total += sc * share
                notes.append(f"{n} {note}")
            if hits_partner:
                sc, _ = damage_score(mv, me, psp, my_lv, partner[1])
                if sc > 0:
                    # never hurt the partner while another attack exists (more healing trips, risk of a faint)
                    total *= 0.01
                    notes.append("아군도 맞음 → 다른 공격이 없을 때만")
            table.append({"slot": slot, "move": key, "target": None, "score": round(total, 1),
                          "why": "전체 공격: " + ", ".join(notes)})
        else:
            for ti, (n, lv, s) in enumerate(foes):
                sc, note = damage_score(mv, me, s, my_lv, lv)
                table.append({"slot": slot, "move": key, "target": ti if doubles else None, "foe": n,
                              "score": round(sc, 1), "why": f"→ {n}: {note}"})
    if not table:
        return None
    best = max(table, key=lambda r: r["score"])
    return dict(best, table=sorted(table, key=lambda r: -r["score"]))


if __name__ == "__main__":
    # the spa double battle from your frames: レアコイル Lv41 vs オコリザル & ゴーリキー Lv39
    r = choose("レアコイル", 41, ["スパーク", "10まんボルト", "ソニックブーム", "でんじは"],
               [("オコリザル", 39), ("ゴーリキー", 39)], partner=("ナッシー", 42))
    print(json.dumps(r, ensure_ascii=False, indent=1))
    r = choose("カビゴン", 39, ["のしかかり", "ずつき", "のろい", "ねむる"], [("ゴーリキー", 39)])
    print(r["move"], r["why"], r["score"])
