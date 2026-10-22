#!/usr/bin/env python3
"""
Battle state for the panel: who is out on each side and which moves they have used.
Fed with the corrected battle text (dark-blue battle text box, the "Xは どうする？" menu, the move list).
Every name goes through the dictionary, so a misread line changes nothing.

quick check:  python battle.py
"""
import json
import re
import time
from pathlib import Path

from jp_text import kana_to_hangul

HERE = Path(__file__).parent
POKEDEX = HERE / "data" / "sprites" / "pokedex.json"
GEN3 = HERE / "auto" / "data" / "gen3.json"        # Gen 3 types (no fairy: ピクシー is normal here)
TYPES = {"normal": ("ノーマル", "노말"), "fire": ("ほのお", "불꽃"), "water": ("みず", "물"), "electric": ("でんき", "전기"),
         "grass": ("くさ", "풀"), "ice": ("こおり", "얼음"), "fighting": ("かくとう", "격투"), "poison": ("どく", "독"),
         "ground": ("じめん", "땅"), "flying": ("ひこう", "비행"), "psychic": ("エスパー", "에스퍼"), "bug": ("むし", "벌레"),
         "rock": ("いわ", "바위"), "ghost": ("ゴースト", "고스트"), "dragon": ("ドラゴン", "드래곤"), "dark": ("あく", "악"),
         "steel": ("はがね", "강철")}
MAX_LOG = 8

PFX = r"(?:(?P<side>あいての|やせいの) )?"


class Battle:
    def __init__(self, gloss):
        self.gloss = gloss
        try:
            self.dex = json.loads(POKEDEX.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.dex = {}
        try:
            self.species = json.loads(GEN3.read_text(encoding="utf-8"))["species"]
        except (OSError, ValueError, KeyError):
            self.species = {}
        self.reset(False)
        self._stash, self._ended = None, 0.0

    def reset(self, active=True):
        self.active, self.wild, self.trainer = active, False, None
        self.foe, self.mine, self.log = None, None, []

    # ------------------------------------------------------------ lookups
    def _poke(self, ja):
        ja = ja.strip()
        g = self.gloss.get(ja)
        if not g or g.get("kind") not in ("pokemon", "custom"):
            return None
        d = self.dex.get(ja, {})
        types = [{"en": t, "ja": TYPES[t][0], "ko": TYPES[t][1]}
                 for t in self.species.get(ja, {}).get("types", []) if t in TYPES]
        return {"ja": ja, "pr": kana_to_hangul(ja.rstrip("♀♂")), "ko": g["ko"],
                "id": d.get("id"), "types": types, "moves": [], "fainted": False}

    def _move(self, ja):
        g = self.gloss.get(ja.strip())
        if not g or g.get("kind") != "move":
            return None
        return {"ja": ja.strip(), "pr": kana_to_hangul(ja.strip()), "ko": g["ko"]}

    def _trainer(self, s):
        from templates import Templates
        ko = Templates(self.gloss).trainer(s)
        return {"ja": s.strip(), "ko": ko or ""}

    def _send_out(self, side, name):
        p = self._poke(name)
        if not p:
            return False
        cur = getattr(self, side)
        if cur and cur["ja"] == p["ja"] and not cur["fainted"]:
            return False
        setattr(self, side, p)
        return True

    def _used(self, side, name, move):
        m = self._move(move)
        if not m:
            return False
        mon = getattr(self, side)
        if mon is None or mon["ja"] != name:
            if not self._send_out(side, name):
                return False
            mon = getattr(self, side)
        if all(x["ja"] != m["ja"] for x in mon["moves"]):
            mon["moves"] = (mon["moves"] + [m])[-4:]
        return True

    # ------------------------------------------------------------ feed
    def end(self):
        """Left the battle screen. Kept for a while: opening the bag or party mid-battle is not the end."""
        if not self.active:
            return False
        self._stash, self._ended = (self.wild, self.trainer, self.foe, self.mine, self.log), time.time()
        self.reset(False)
        return True

    def feed(self, ja, in_battle, ko=None, compact=False, pr=""):
        """ja: corrected line. in_battle: shown in the battle text box / battle menu. -> state changed?"""
        if not in_battle:
            return self.end()
        s = re.sub(r"\s+", " ", ja.strip())
        starts = re.fullmatch(r"(?:あ！ )?やせいの (\S+?)が とびだしてきた！|(.+?)が しょうぶを しかけてきた！", s)
        changed = False
        if not self.active:
            self.reset(True)
            if not starts and self._stash and time.time() - self._ended < 120:
                self.wild, self.trainer, self.foe, self.mine, self.log = self._stash   # back from bag/party
            changed = True
        m = re.fullmatch(r"(?:あ！ )?やせいの (\S+?)が とびだしてきた！", s)
        matched = True
        if m:
            self.reset(True)
            self.wild = True
            self._send_out("foe", m.group(1))
            changed = True
        elif (m := re.fullmatch(r"(.+?)が しょうぶを しかけてきた！", s)):
            self.reset(True)
            self.trainer = self._trainer(m.group(1))
            changed = True
        elif (m := re.fullmatch(r"(.+?)は (\S+?)を くりだ(?:した！|そうと している)", s)):
            if self.trainer is None:
                self.trainer = self._trainer(m.group(1))
            changed |= self._send_out("foe", m.group(2))
        elif (m := re.fullmatch(r"(?:ゆけっ|いけっ|がんばれ|いってこい|いけ)！ (\S+?)！", s)):
            changed |= self._send_out("mine", m.group(1))
        elif (m := re.fullmatch(r"(\S+?)は どうする？", s)):
            changed |= self._send_out("mine", m.group(1))
        elif (m := re.fullmatch(PFX + r"(?P<p>\S+?)の (?P<m>\S+?)(?: こうげき)?！", s)):
            changed |= self._used("foe" if m["side"] else "mine", m["p"], m["m"])
        elif (m := re.fullmatch(PFX + r"(?P<p>\S+?)は (?P<m>\S+?)を つかった！", s)):
            changed |= self._used("foe" if m["side"] else "mine", m["p"], m["m"])
        elif (m := re.fullmatch(PFX + r"(?P<p>\S+?)は たおれた！", s)):
            side = "foe" if m["side"] else "mine"
            mon = getattr(self, side)
            if mon and mon["ja"] == m["p"]:
                mon["fainted"] = True
                changed = True
        else:
            matched = False
        if not matched:                               # any other battle line still says who is out
            m = re.search(r"(?:あいての|やせいの) (\S+?)(?:の|は|を|が|に|も) ", s + " ")
            if m and self._poke(m.group(1)) and (self.foe is None or self.foe["ja"] != m.group(1)):
                changed |= self._send_out("foe", m.group(1))
            m = re.match(r"(\S+?)(?:の|は|を|が) ", s)
            if m and not s.startswith(("あいての", "やせいの")) and self._poke(m.group(1)) \
                    and (self.foe is None or self.foe["ja"] != m.group(1)) \
                    and (self.mine is None or self.mine["ja"] != m.group(1)):
                changed |= self._send_out("mine", m.group(1))
        if compact:
            self.log = (self.log + [{"ja": s, "pr": pr, "ko": ko or ""}])[-MAX_LOG:]
            changed = True
        return changed

    def set_ko(self, ja, ko):
        """Translation arrived later (LLM): fill the log row."""
        for row in self.log:
            if row["ja"] == ja and not row["ko"]:
                row["ko"] = ko

    def state(self):
        return {"type": "battle", "active": self.active, "wild": self.wild, "trainer": self.trainer,
                "foe": self.foe, "mine": self.mine, "log": self.log}


if __name__ == "__main__":
    from jp_text import Hinter
    b = Battle(Hinter().gloss)
    for line in ["ロケットだんの したっぱが しょうぶを しかけてきた！", "ロケットだんの したっぱは ズバットを くりだした！",
                 "ゆけっ！ リザードン！", "リザードンは どうする？", "リザードンの かえんほうしゃ！", "あいての ズバットの きゅうけつ！",
                 "あいての ズバットの ちょうおんぱ！", "あいての ズバットは たおれた！"]:
        b.feed(line, True, compact=True)
    print(json.dumps(b.state(), ensure_ascii=False, indent=1))
