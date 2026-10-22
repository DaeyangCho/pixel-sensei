#!/usr/bin/env python3
"""
Fixed battle / system messages -> Korean without the LLM (instant, and never "won" -> "lost").
FRLG battle text is formulaic: "<name>は たおれた！", "<name>の <move>！" ... Every slot must resolve
through the dictionary (Pokemon / move / item / trainer class / your names); otherwise -> None and the LLM
handles the line as before.

quick check:  python templates.py "あいての サンドパンの きりさく こうげき！"
"""
import re
import sys

from jp_text import TRAINER_CLASSES, kana_to_hangul, wdist

# ---------------------------------------------------------------- Korean particles
_DIGIT_FINAL = {"0": "ㅇ", "1": "ㄹ", "2": "", "3": "ㅁ", "4": "", "5": "", "6": "ㄱ", "7": "ㄹ", "8": "ㄹ",
                "9": ""}
PAIRS = {"은": ("은", "는"), "이": ("이", "가"), "을": ("을", "를"), "과": ("과", "와"), "으로": ("으로", "로"),
         "아": ("아", "야")}


def _final(word):
    """'' (no batchim), 'ㄹ', or 'x' (other batchim) of the last pronounced char."""
    for ch in reversed(word.strip()):
        code = ord(ch) - 0xAC00
        if 0 <= code < 11172:
            f = code % 28
            return "" if f == 0 else "ㄹ" if f == 8 else "x"
        if ch.isdigit():
            f = _DIGIT_FINAL[ch]
            return "ㄹ" if f == "ㄹ" else "x" if f else ""
        if ch.isalpha():
            return ""
    return ""


def josa(word, kind):
    with_final, without = PAIRS[kind]
    f = _final(word)
    if kind == "으로":
        return without if f in ("", "ㄹ") else with_final
    return with_final if f else without


def J(word, kind):
    return word + josa(word, kind)


# ---------------------------------------------------------------- slot resolution
STATS = {"こうげき": "공격", "こうげきりょく": "공격", "ぼうぎょ": "방어", "ぼうぎょりょく": "방어",
         "とくこう": "특수공격", "とくぼう": "특수방어", "すばやさ": "스피드", "めいちゅうりつ": "명중률",
         "かいひりつ": "회피율"}
_CLASS_KEYS = sorted(TRAINER_CLASSES, key=len, reverse=True)


def _person_hangul(kana):
    return kana_to_hangul(kana.replace("ー", ""))          # テリー -> 테리 (not 테리이)


class Templates:
    def __init__(self, gloss):
        self.gloss = gloss

    def _g(self, s, kinds):
        g = self.gloss.get(s)
        return g["ko"] if g and g.get("kind") in kinds else None

    def poke(self, s):
        return self._g(s, ("pokemon", "custom"))           # custom: nicknames you put in data/names.json

    def move(self, s):
        return self._g(s, ("move",))

    def item(self, s):
        m = re.fullmatch(r"(わざ|ひでん)マシン ?(\d+)(?: ?「(.+)」)?", s)
        if m:
            kind = "기술머신" if m.group(1) == "わざ" else "비전머신"
            mv = self.move(m.group(3)) if m.group(3) else ""
            if m.group(3) and not mv:
                return None
            return f"{kind}{m.group(2)}" + (f" '{mv}'" if mv else "")
        return self._g(s, ("item",))

    def me(self, s):
        return self._g(s, ("player",))

    def trainer(self, s):
        """'ジャグラーの テリー', 'ロケットだんの したっぱ', rival / player names."""
        s = s.strip()
        direct = self._g(s, ("player", "rival", "custom", "name", "trainer"))
        if direct:
            return direct
        for k in _CLASS_KEYS:
            if s == k:
                return TRAINER_CLASSES[k]
        m = re.fullmatch(r"(.+?)の ?(\S+)", s)
        if not m:
            return None
        cls, name = m.group(1), m.group(2)
        cls_ko = TRAINER_CLASSES.get(cls)
        if cls_ko is None:                                  # OCR slip in the class (ジャクラー)
            scored = sorted((wdist(cls, k), k) for k in TRAINER_CLASSES if abs(len(k) - len(cls)) <= 1)
            if scored and scored[0][0] <= 0.6 and (len(scored) < 2 or scored[1][0] >= scored[0][0] + 0.3):
                cls_ko = TRAINER_CLASSES[scored[0][1]]
        if not re.fullmatch(r"[ぁ-んァ-ヴー]+", name) or not re.fullmatch(r"[ぁ-んァ-ヴー ]+", cls):
            return None
        if cls_ko is None:                                  # class not in the list: its sound, never a guess
            cls_ko = _person_hangul(cls)
        known = self._g(name, ("name", "rival", "player", "custom"))   # カツラ -> 강연, not 카츠라
        return f"{cls_ko} {known or _person_hangul(name)}"

    def any_mon(self, s):
        """'あいての X' or 'X' -> (prefix, korean name)."""
        opp = s.startswith("あいての ")
        name = self.poke(s[5:] if opp else s)
        return (("상대 " if opp else "") + name) if name else None

    # ------------------------------------------------------------ rules
    def translate(self, ja):
        s = re.sub(r"\s+", " ", ja.strip())
        for pat, fn in RULES:
            m = re.fullmatch(pat, s)
            if m:
                try:
                    out = fn(self, m)
                except (KeyError, TypeError):
                    out = None
                if out:
                    return out
        return None


P = r"(?P<p>(?:あいての )?\S+?)"          # Pokemon, maybe "あいての X"
T = r"(?P<t>.+?)"                         # trainer (may contain a space)

FIXED = {
    "こうかは ばつぐんだ！": "효과가 굉장했다!",
    "こうかは いまひとつの ようだ": "효과가 별로인 듯하다…",
    "こうかは いまひとつの ようだ…": "효과가 별로인 듯하다…",
    "きゅうしょに あたった！": "급소에 맞았다!",
    "いちげき ひっさつ！": "일격필살!",
    "しかし うまく きまらなかった！！": "그러나 잘 되지 않았다!",
    "しかし うまく きまらなかった！": "그러나 잘 되지 않았다!",
    "うまく にげきれた！": "무사히 도망쳤다!",
    "にげられない！": "도망칠 수 없다!",
    "だめだ！ トレーナーとの しょうぶから にげることは できない！":
        "안 돼! 트레이너와의 승부에서는 도망칠 수 없다!",
    "しかし あいては こうげきを かわした！": "그러나 상대는 공격을 피했다!",
}


def _fx(t, m):
    return FIXED.get(m.group(0))


RULES = [
    (r".+", _fx),
    (rf"{P}の (?P<m>\S+?)(?: こうげき)?！",
     lambda t, m: (lambda a, b: a and b and f"{a}의 {b}!")(t.any_mon(m["p"]), t.move(m["m"]))),
    (rf"{P}は (?P<m>\S+?)を つかった！",
     lambda t, m: (lambda a, b: a and b and f"{J(a, '은')} {J(b, '을')} 사용했다!")(t.any_mon(m["p"]), t.move(m["m"]))),
    (rf"{P}は たおれた！",
     lambda t, m: (lambda a: a and f"{J(a, '은')} 쓰러졌다!")(t.any_mon(m["p"]))),
    (rf"{P}は (?P<a>おおめに )?(?P<n>\d+) ?けいけんちを もらった！",
     lambda t, m: (lambda a: a and f"{J(a, '은')} {'많이 ' if m['a'] else ''}{m['n']} 경험치를 얻었다!")(t.poke(m["p"]))),
    (rf"{P}は レベル ?(?P<n>\d+) ?に あがった！",
     lambda t, m: (lambda a: a and f"{J(a, '은')} 레벨 {J(m['n'], '으로')} 올랐다!")(t.poke(m["p"]))),
    (rf"{T}は {P}を くりだした！",
     lambda t, m: (lambda a, b: a and b and f"{J(a, '은')} {J(b, '을')} 내보냈다!")(t.trainer(m["t"]), t.poke(m["p"]))),
    (rf"{T}は {P}を くりだそうと している",
     lambda t, m: (lambda a, b: a and b and f"{J(a, '은')} {J(b, '을')} 내보내려 하고 있다")(t.trainer(m["t"]), t.poke(m["p"]))),
    (rf"{T}が しょうぶを しかけてきた！",
     lambda t, m: (lambda a: a and f"{J(a, '이')} 승부를 걸어왔다!")(t.trainer(m["t"]))),
    (rf"{T} ?との しょうぶに かった！",
     lambda t, m: (lambda a: a and f"{J(a, '과')}의 승부에서 이겼다!")(t.trainer(m["t"]))),
    (rf"(?:ゆけっ|いけっ)！ {P}！",
     lambda t, m: (lambda a: a and f"가랏! {a}!")(t.poke(m["p"]))),
    (rf"がんばれ！ {P}！",
     lambda t, m: (lambda a: a and f"힘내! {a}!")(t.poke(m["p"]))),
    (rf"{P}(?: よくやった！)? もどれ！",
     lambda t, m: (lambda a: a and f"{a}, {'잘했어! ' if 'よくやった' in m[0] else ''}돌아와!")(t.poke(m["p"]))),
    (rf"{P} もういい！ もどれ！",
     lambda t, m: (lambda a: a and f"{a}, 이제 됐어! 돌아와!")(t.poke(m["p"]))),
    (rf"{P}は どうする？",
     lambda t, m: (lambda a: a and f"{J(a, '은')} 무엇을 할까?")(t.poke(m["p"]))),
    (rf"しかし {P}の こうげきは はずれた！",
     lambda t, m: (lambda a: a and f"그러나 {a}의 공격은 빗나갔다!")(t.any_mon(m["p"]))),
    (rf"{P}の (?P<s>\S+?)が (?P<adv>ぐーんと |がくっと |がくーんと )?(?P<v>あがった|さがった)！",
     lambda t, m: (lambda a, s: a and s and
                   f"{a}의 {J(s, '이')} {'크게 ' if m['adv'] else ''}{'올랐다' if m['v'] == 'あがった' else '떨어졌다'}!")(
                       t.any_mon(m["p"]), STATS.get(m["s"]))),
    (rf"{P}は (?P<m>\S+?)を おぼえた！",
     lambda t, m: (lambda a, b: a and b and f"{J(a, '은')} {J(b, '을')} 배웠다!")(t.poke(m["p"]), t.move(m["m"]))),
    (rf"(?:あっ？?！? ?|あ！ )?やせいの (?P<p>\S+?)が とびだしてきた！",
     lambda t, m: (lambda a: a and f"앗! 야생의 {J(a, '이')} 튀어나왔다!")(t.poke(m["p"]))),
    (rf"やったー！ (?P<p>\S+?)を つかまえたぞ！",
     lambda t, m: (lambda a: a and f"신난다! {J(a, '을')} 잡았다!")(t.poke(m["p"]))),
    (rf"(?P<w>\S+?)は (?P<x>.+?)を みつけた！",
     lambda t, m: (lambda w, x: w and x and f"{J(w, '은')} {J(x, '을')} 찾았다!")(t.me(m["w"]), t.item(m["x"]))),
    (rf"(?P<w>\S+?)は (?P<x>.+?)を てにいれた！",
     lambda t, m: (lambda w, x: w and x and f"{J(w, '은')} {J(x, '을')} 손에 넣었다!")(t.me(m["w"]), t.item(m["x"]))),
    (rf"(?P<w>\S+?)は しょうきんとして (?P<n>\d+) ?円 ?てにいれた！",
     lambda t, m: (lambda w: w and f"{J(w, '은')} 상금으로 {m['n']}원을 손에 넣었다!")(t.me(m["w"]))),
    (rf"{P}は めを さました！",
     lambda t, m: (lambda a: a and f"{J(a, '은')} 잠에서 깼다!")(t.any_mon(m["p"]))),
    (rf"{P}は ねむってしまった！",
     lambda t, m: (lambda a: a and f"{J(a, '은')} 잠들어 버렸다!")(t.any_mon(m["p"]))),
    (rf"{P}は ぐうぐう ねむっている",
     lambda t, m: (lambda a: a and f"{J(a, '은')} 쿨쿨 잠들어 있다")(t.any_mon(m["p"]))),
    (rf"{P}は こんらんした！",
     lambda t, m: (lambda a: a and f"{J(a, '은')} 혼란에 빠졌다!")(t.any_mon(m["p"]))),
    (rf"{P}は どくを あびた！",
     lambda t, m: (lambda a: a and f"{J(a, '은')} 독에 걸렸다!")(t.any_mon(m["p"]))),
    (rf"{P}は まひして わざが でにくくなった！",
     lambda t, m: (lambda a: a and f"{J(a, '은')} 마비되어 기술을 쓰기 어려워졌다!")(t.any_mon(m["p"]))),
    (rf"{P}は どくの ダメージを うけている！",
     lambda t, m: (lambda a: a and f"{J(a, '은')} 독 데미지를 입고 있다!")(t.any_mon(m["p"]))),
]


if __name__ == "__main__":
    from jp_text import Hinter
    tp = Templates(Hinter().gloss)
    for line in sys.argv[1:]:
        print(line, "->", tp.translate(line))
