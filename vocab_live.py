"""Live word rail: find the game's words in a line, count how often you have seen them, remember what you know.

FireRed text is kana only, so a general morphological analyser cuts it badly (けいけんち -> 家+敬虔,
しょうきん -> 松琴, しょうぶに かった -> 買う). Words are matched against a hand-made game lexicon instead
(lexicon_src.txt, ~280 words with every conjugated form generated), longest match first; Pokemon /
move / item / place names are skipped (the battle view shows those).

    v = Vocab(gloss)                 # loads data/progress.json (seeded from logs/lines.jsonl the first time)
    v.find("あいての ラッタは たおれた！")   -> ["あいて", "たおれる"]
    v.seen_card(ids, ja, ko)         # +1 seen for each word (once per card), keeps a few example sentences
    v.set(id, status="known")        # / star=True
"""
import json
import re
import threading
import time
from pathlib import Path

HERE = Path(__file__).parent
SRC = HERE / "lexicon_src.txt"
PROGRESS = HERE / "data" / "progress.json"
LINES = HERE / "logs" / "lines.jsonl"
NAME_KINDS = ("pokemon", "move", "item", "name", "trainer", "player", "rival", "custom")
BASIC = {"する", "いる", "ある", "くる", "いく", "なる", "みる", "いう", "でる"}   # known from day one

ROWS = {"う": "わいうえお", "く": "かきくけこ", "ぐ": "がぎぐげご", "す": "さしすせそ", "つ": "たちつてと",
        "ぬ": "なにぬねの", "ぶ": "ばびぶべぼ", "む": "まみむめも", "る": "らりるれろ"}
ONBIN = {"う": ("っ", "た"), "つ": ("っ", "た"), "る": ("っ", "た"), "ぬ": ("ん", "だ"), "ぶ": ("ん", "だ"),
         "む": ("ん", "だ"), "く": ("い", "た"), "ぐ": ("い", "だ"), "す": ("し", "た")}
IRREGULAR = {
    "する": ["する", "して", "しない", "しま", "しよう", "すれ", "させ", "され"],
    "くる": ["くる", "こない", "きま", "こよう", "くれば"],
    "いる": ["いる", "いない", "いまし", "いれば"],
    "ある": ["ある", "あった", "あって", "あり", "あれば"],
    "でる": ["でる", "でた", "でて", "でない", "でま", "でれ", "でよう"],
    "いく": ["いく", "いった", "いって", "いか", "いき", "いけ", "いこう"],
    "いい": ["いい"],
}


def forms_of(ja, pos):
    if ja in IRREGULAR:
        return list(IRREGULAR[ja])
    out = [ja]
    if pos.startswith("v1"):
        stem = ja[:-1]
        out += [stem] if len(stem) >= 2 else [stem + e for e in ("る", "た", "て", "ない", "ま", "れ", "よう", "させ", "られ")]
    elif pos.startswith("v5"):
        base, last = ja[:-1], ja[-1]
        row = ROWS.get(last, "")
        oc, ta = ONBIN.get(last, ("っ", "た"))
        te = "て" if ta == "た" else "で"
        if len(base) >= 2:
            out += [base + c for c in row] + [base + oc]
        else:                                             # 1-kana stem: only long forms (no clash with other words)
            out += [base + oc + ta, base + oc + te, base + row[1:2] + "ま", base + row[:1] + "な"]
    elif pos == "adj":
        base = ja[:-1]
        out += [base + e for e in ("い", "く", "かっ", "けれ", "さ", "そう")]
    return out


class Vocab:
    def __init__(self, gloss=None, lines_path=LINES, progress_path=PROGRESS, src=SRC):
        self.lock = threading.Lock()
        self.lex, self.order, self.index = {}, [], {}
        self._load_lex(src)
        self.names = {}
        for k, v in (gloss or {}).items():
            if v.get("kind") in NAME_KINDS and len(k) >= 2:
                self.names.setdefault(k[0], set()).add(k)
        self.progress_path = Path(progress_path)
        self.prog = self._load_progress(lines_path)
        self._dirty, self._saved = False, time.time()

    # ------------------------------------------------------------ lexicon
    def _load_lex(self, src):
        group = ""
        for raw in Path(src).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("@"):
                group = line[1:]
                continue
            if line.startswith("!"):                       # blockers: look like a word but are not ('とかいって' = と言って)
                for fm in line[1:].split(","):
                    if fm.strip():
                        self.index[fm.strip()] = None
                continue
            f = (line.split("|") + [""] * 7)[:7]
            head, pos, kanji, ko, kr, same, note = [x.strip() for x in f]
            ja, _, extra = head.partition("=")
            if ja in self.lex:
                continue
            forms = forms_of(ja, pos) + [x.strip() for x in extra.split(",") if x.strip()]
            self.lex[ja] = {"ja": ja.replace("めをさます", "めを さます"), "kanji": kanji, "ko": ko, "kr": kr,
                            "same": same == "1", "note": note, "g": group, "pos": pos}
            self.order.append(ja)
            for fm in dict.fromkeys(forms):
                if len(fm.replace(" ", "")) >= 2 and fm not in self.index:
                    self.index[fm] = ja
        self.by_first = {}
        for fm, ja in self.index.items():
            self.by_first.setdefault(fm[0], []).append(fm)
        for k in self.by_first:
            self.by_first[k].sort(key=len, reverse=True)

    def find(self, text):
        """Word ids in reading order (each once). Longest match wins; names are stepped over."""
        text = text or ""
        out, i, n = [], 0, len(text)
        while i < n:
            start = i == 0 or text[i - 1] in " 　、。！？…「」『』・"
            name = next((nm for nm in sorted(self.names.get(text[i], ()), key=len, reverse=True)
                         if text.startswith(nm, i)), None) if text[i] in self.names else None
            word = None
            for fm in self.by_first.get(text[i], ()):
                if text.startswith(fm, i) and (start or len(fm) >= 3):
                    word = fm
                    break
            if name and (not word or len(name) > len(word)):
                i += len(name)
                continue
            if word:
                ja = self.index[word]
                if ja is not None and ja not in out:
                    out.append(ja)
                i += len(word)
                continue
            i += 1
        return out

    # ------------------------------------------------------------ progress
    def _load_progress(self, lines_path):
        try:
            return json.loads(self.progress_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        prog = {}
        try:                                              # first run: what you have already seen in the logs
            for line in Path(lines_path).read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                for ja in self.find(r.get("ja", "")):
                    p = prog.setdefault(ja, {"seen": 0, "status": "", "star": False, "ex": []})
                    p["seen"] += 1
                    if r.get("ko") and len(p["ex"]) < 3 and r.get("src") != "template" \
                            and all(e[0] != r["ja"] for e in p["ex"]):
                        p["ex"].append([r["ja"], r["ko"]])
        except OSError:
            pass
        for ja in BASIC:
            if ja in self.lex:
                prog.setdefault(ja, {"seen": 0, "status": "", "star": False, "ex": []})["status"] = "known"
        self.progress_path.parent.mkdir(parents=True, exist_ok=True)
        self.progress_path.write_text(json.dumps(prog, ensure_ascii=False), encoding="utf-8")
        return prog

    def _p(self, ja):
        return self.prog.setdefault(ja, {"seen": 0, "status": "known" if ja in BASIC else "", "star": False, "ex": []})

    def seen_card(self, ids, ja=None, ko=None):
        """Once per card: +1 seen for each word; remember the sentence as an example. -> changed ids."""
        with self.lock:
            for w in ids:
                p = self._p(w)
                p["seen"] += 1
                if ja and ko and all(e[0] != ja for e in p["ex"]):
                    p["ex"] = ([[ja, ko]] + p["ex"])[:3]
            self._dirty = True
            self._maybe_save()
        return list(ids)

    def set(self, ja, status=None, star=None):
        if ja not in self.lex:
            return None
        with self.lock:
            p = self._p(ja)
            if status is not None:
                p["status"] = status if status in ("known", "learning") else ""
            if star is not None:
                p["star"] = bool(star)
            self._dirty = True
            self._maybe_save(force=True)
            return p

    def _maybe_save(self, force=False):
        if self._dirty and (force or time.time() - self._saved > 20):
            tmp = self.progress_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.prog, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.progress_path)
            self._dirty, self._saved = False, time.time()

    def save(self):
        with self.lock:
            self._maybe_save(force=True)

    def snapshot(self):
        """Everything the panel needs once: the lexicon and your progress."""
        with self.lock:
            return {"type": "vocab_all", "lex": {k: self.lex[k] for k in self.order},
                    "prog": {k: v for k, v in self.prog.items() if k in self.lex}}

    def entry(self, ja):
        with self.lock:
            return {"type": "vocab_prog", "id": ja, "prog": dict(self._p(ja))}


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(HERE))
    from jp_text import Hinter
    v = Vocab(Hinter().gloss, progress_path="/tmp/progress_test.json")
    print(len(v.lex), "words,", len(v.index), "forms")
    for t in ["あいての ラッタは たおれた！", "ピジョンは 900 けいけんちを もらった！",
              "カラテおうの タクミ との しょうぶに かった！", "おしえんは しょうきんとして 960円 てにいれた！",
              "この くらいまで とかいから はなれると すごい ほしぞらが みられるんだ",
              "やせいの ラッタは まひして わざが でにくくなった！", "こうかは ばつぐんだ！",
              "ニドキング もういい！ もどれ！", "おまちどおさまでした！ おあずかりした ポケモンは みんな げんきに なりましたよ！",
              "やせいの パウワウは めを さました！", "レアコイルの スパーク！"]:
        print(t, "->", v.find(t))
