#!/usr/bin/env python3
"""
Japanese text helpers for the Pokemon FR study tool (no LLM here, all deterministic).
  - Glossary: Pokemon / move / item names (PokeAPI) + hand-written game vocab
  - correct_and_hint(): OCR-confusion-aware name correction + hint lines for the LLM
  - to_hangul(): kana -> Korean-letter pronunciation (particles は/へ read as わ/え)

build glossary once (downloads PokeAPI csv from GitHub):
  python jp_text.py build
quick check:
  python jp_text.py check "あ！ やせいの ディウダが とびだしてきた！"
"""
import csv
import json
import re
import sys
import unicodedata
import urllib.request
from pathlib import Path

DATA = Path(__file__).with_name("data")
GLOSSARY = DATA / "glossary.json"
POKEAPI = "https://raw.githubusercontent.com/PokeAPI/pokeapi/master/data/v2/csv/"

# ---------------------------------------------------------------- game vocab
# kana -> (kanji/japanese form, korean). Edit freely; this beats the tokenizer on game words.
VOCAB = {
    "やせい": ("野生", "야생"), "けいけんち": ("経験値", "경험치"), "ひんし": ("瀕死", "기절(빈사)"),
    "わざ": ("技", "기술"), "どうぐ": ("道具", "도구"), "あいて": ("相手", "상대"),
    "こうげき": ("攻撃", "공격"), "ぼうぎょ": ("防御", "방어"), "とくこう": ("特攻", "특수공격"),
    "とくぼう": ("特防", "특수방어"), "すばやさ": ("素早さ", "스피드"), "めいちゅう": ("命中", "명중"),
    "きゅうしょ": ("急所", "급소"), "ばつぐん": ("抜群", "굉장함"), "いまひとつ": ("いまひとつ", "별로임"),
    "しょうぶ": ("勝負", "승부"), "はかせ": ("博士", "박사"), "ずかん": ("図鑑", "도감"),
    "かいふく": ("回復", "회복"), "どく": ("毒", "독"), "まひ": ("麻痺", "마비"),
    "ねむり": ("眠り", "잠듦"), "やけど": ("火傷", "화상"), "こおり": ("氷", "얼음"),
    "こんらん": ("混乱", "혼란"), "しんか": ("進化", "진화"), "ほのお": ("炎", "불꽃"),
    "みず": ("水", "물"), "くさ": ("草", "풀"), "でんき": ("電気", "전기"), "じめん": ("地面", "땅"),
    "いわ": ("岩", "바위"), "ひこう": ("飛行", "비행"), "むし": ("虫", "벌레"), "かくとう": ("格闘", "격투"),
    "はがね": ("鋼", "강철"), "あく": ("悪", "악"), "おこづかい": ("お小遣い", "용돈"),
    "しょうきん": ("賞金", "상금"), "どうろ": ("道路", "도로"), "もり": ("森", "숲"),
    "どうくつ": ("洞窟", "동굴"), "ロケットだん": ("ロケット団", "로켓단"),
    "トレーナー": ("トレーナー", "트레이너"), "ジム": ("ジム", "체육관"), "リーダー": ("リーダー", "관장"),
    "バッジ": ("バッジ", "배지"), "ポケモン": ("ポケモン", "포켓몬"),
    "ポケモンセンター": ("ポケモンセンター", "포켓몬센터"),
    "フレンドリィショップ": ("フレンドリィショップ", "프렌들리숍"), "パソコン": ("パソコン", "컴퓨터"),
    "レベル": ("レベル", "레벨"), "タイプ": ("タイプ", "타입"), "ニックネーム": ("ニックネーム", "닉네임"),
    "を つかった": ("を 使った", "~을 사용했다"), "を くりだした": ("を 繰り出した", "~을 내보냈다"),
    "を おぼえた": ("を 覚えた", "~을 배웠다"), "とびだしてきた": ("飛び出してきた", "튀어나왔다"),
    "つかまえた": ("捕まえた", "잡았다"), "たおした": ("倒した", "쓰러뜨렸다"),
    "にげられない": ("逃げられない", "도망칠 수 없다"), "うまく にげきれた": ("うまく 逃げ切れた", "무사히 도망쳤다"),
    "おまちどおさま": ("お待ち遠様", "오래 기다리셨습니다"), "おあずかり": ("お預かり", "맡음/맡아 드림"),
    "げんき": ("元気", "건강함/기운"), "いらっしゃいませ": ("いらっしゃいませ", "어서 오세요"),
    "わざマシン": ("技マシン", "기술머신"), "ひでんマシン": ("秘伝マシン", "비전머신"),
    "てにいれた": ("手に入れた", "손에 넣었다"), "みつけた": ("見つけた", "찾았다"),
    "あずかり": ("預かり", "맡김"),
}

# Trainer classes (FRLG). Korean names are the usual later-game ones (FRLG itself had no Korean release).
TRAINER_CLASSES = {
    "むしとりしょうねん": "곤충채집 소년", "たんパンこぞう": "반바지 꼬마", "ミニスカート": "미니스커트",
    "キャンプボーイ": "캠프보이", "ピクニックガール": "피크닉걸", "ふなのり": "뱃사람", "つりびと": "낚시꾼",
    "かいパンやろう": "수영팬티 녀석", "ビキニのおねえさん": "비키니 아가씨", "やまおとこ": "등산가",
    "ポケモンマニア": "포켓몬 마니아", "りかけいのおとこ": "이과계 남자", "スキンヘッズ": "스킨헤드",
    "ぼうそうぞく": "폭주족", "ジャグラー": "저글러", "ギャンブラー": "갬블러", "サイキッカー": "초능력자",
    "からておう": "가라테왕", "エリートトレーナー": "엘리트 트레이너", "ジェントルマン": "신사",
    "おじょうさま": "아가씨", "じゅくがえり": "학원생", "ふたごちゃん": "쌍둥이", "とりつかい": "새조련사",
    "ロッカー": "로커", "きのみおじさん": "나무열매 아저씨", "カップル": "커플", "ハイキング": "등산객",
    "ルーキー": "신인 트레이너", "ポケモンブリーダー": "포켓몬 브리더", "ボーイスカウト": "보이스카우트",
    "ガールスカウト": "걸스카우트", "ちょうのうりょくしゃ": "초능력자", "ジムリーダー": "체육관 관장",
    "してんのう": "사천왕", "チャンピオン": "챔피언", "ライバル": "라이벌",
    "ロケットだんの したっぱ": "로켓단 조무래기", "ロケットだんのしたっぱ": "로켓단 조무래기",
    "ロケットだん": "로켓단", "かんぶ": "간부",
    "もうじゅうつかい": "맹수조련사", "カラテおう": "가라테왕", "きとうし": "기도사", "けんきゅういん": "연구원",
    "おとなのおねえさん": "어른 누나", "どろぼう": "도둑", "アロマなおねえさん": "아로마 누나", "いせきマニア": "유적 마니아",
    "えかきさん": "화가", "うきわボーイ": "튜브 소년", "うきわガール": "튜브 소녀", "ギタリスト": "기타리스트",
    "こうじいん": "작업원", "ポケモンレンジャー": "포켓몬 레인저", "きょうだい": "남매",
}

# Kanto places / characters (not in PokeAPI name tables). Exact names are never "corrected".
EXTRA_NAMES = {
    "マサラタウン": "태초마을", "トキワシティ": "상록시티", "ニビシティ": "회색시티", "ハナダシティ": "블루시티",
    "クチバシティ": "갈색시티", "シオンタウン": "보라타운", "タマムシシティ": "무지개시티",
    "ヤマブキシティ": "노랑시티", "セキチクシティ": "연분홍시티", "グレンじま": "홍련섬",
    "マサラ": "태초", "トキワ": "상록", "ニビ": "회색", "ハナダ": "블루", "クチバ": "갈색", "シオン": "보라",
    "タマムシ": "무지개", "ヤマブキ": "노랑", "セキチク": "연분홍", "グレン": "홍련",
    "オーキド": "오박사", "サカキ": "비주기", "タケシ": "웅", "カスミ": "이슬", "マチス": "마티스",
    "エリカ": "민화", "キョウ": "독수", "ナツメ": "초련", "カツラ": "강연", "ロケット": "로켓",
    "グリーン": "그린", "レッド": "레드", "シルフ": "실프", "サントアンヌ": "상트안느",
}
PARTICLES = "がをはにのでともへや"
KIND = {"pokemon": "포켓몬", "move": "기술", "item": "도구", "name": "지명/인물", "word": "단어",
        "player": "주인공(플레이어) 이름 — 번역하지 말고 이 표기 그대로", "rival": "라이벌 이름 — 이 표기 그대로",
        "custom": "게임 속 이름 — 이 표기 그대로", "trainer": "트레이너 종류"}

# Names you chose in the game live in data/names.json (see data/names.example.json):
#   {"player": {"<주인공 이름>": "<한글>"}, "rival": {"グリーン": "그린"}, "custom": {"<닉네임>": "<한글>"}}
# A flat {"<name>": "<한글>"} file still works (all treated as custom names).
PLAYER_NAMES = {}
RIVAL_NAMES = {"グリーン": "그린"}


# ---------------------------------------------------------------- glossary build
def build():
    DATA.mkdir(exist_ok=True)
    tables = {"pokemon": ("pokemon_species_names", 386), "move": ("move_names", 354),
              "item": ("item_names", None)}
    gloss = {}
    for kind, (name, max_id) in tables.items():
        url = POKEAPI + name + ".csv"
        print("download", url)
        raw = urllib.request.urlopen(url, timeout=30).read().decode("utf-8")
        rows = {}
        for r in csv.DictReader(raw.splitlines()):
            i = int(r[next(iter(r))])
            if max_id and i > max_id:
                continue
            rows.setdefault(i, {})[r["local_language_id"]] = r["name"]
        for v in rows.values():
            ja, ko = v.get("1"), v.get("3")
            if ja and ko:
                gloss.setdefault(ja, {"ko": ko, "kind": kind})
                if ja[-1] in "♀♂":                       # ニドラン♀ -> also ニドラン
                    gloss.setdefault(ja[:-1], {"ko": ko.rstrip("♀♂"), "kind": kind})
    GLOSSARY.write_text(json.dumps(gloss, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"saved {len(gloss)} names -> {GLOSSARY}")


def load_glossary():
    if not GLOSSARY.exists():
        raise SystemExit("glossary missing: run `python jp_text.py build` first")
    return json.loads(GLOSSARY.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- OCR-aware matching
def _base(c):
    d = unicodedata.normalize("NFD", c)
    return d[0] if d else c

SMALL = dict(zip("ァィゥェォッャュョヮぁぃぅぇぉっゃゅょゎ", "アイウエオツヤユヨワあいうえおつやゆよわ"))
VISUAL = {frozenset(p) for p in ["ウク", "シツ", "ソン", "ワク", "ヌス", "チテ", "ロコ", "ラヲ", "ハパ", "ペヘ", "ニ二"]}


def _sub_cost(a, b):
    if a == b:
        return 0.0
    ba, bb = SMALL.get(_base(a), _base(a)), SMALL.get(_base(b), _base(b))
    if ba == bb:
        return 0.3                       # dakuten / handakuten / small-kana slip
    if frozenset((ba, bb)) in VISUAL:
        return 0.5                       # shape confusions seen in pixel fonts
    return 1.0


def _indel_cost(c):
    return 0.5 if c in "ーッっ" else 1.0


def wdist(s, t):
    m, n = len(s), len(t)
    prev = [0.0] * (n + 1)
    for j in range(1, n + 1):
        prev[j] = prev[j - 1] + _indel_cost(t[j - 1])
    for i in range(1, m + 1):
        cur = [prev[0] + _indel_cost(s[i - 1])] + [0.0] * n
        for j in range(1, n + 1):
            cur[j] = min(prev[j] + _indel_cost(s[i - 1]), cur[j - 1] + _indel_cost(t[j - 1]),
                         prev[j - 1] + _sub_cost(s[i - 1], t[j - 1]))
        prev = cur
    return prev[n]


KATA_RUN = re.compile(r"[ァ-ヴー]{2,}")


class Hinter:
    def __init__(self):
        self.gloss = load_glossary()
        names_file = DATA / "names.json"
        extra = json.loads(names_file.read_text(encoding="utf-8")) if names_file.exists() else {}
        if any(isinstance(v, dict) for v in extra.values()):
            PLAYER_NAMES.update(extra.get("player", {}))
            RIVAL_NAMES.update(extra.get("rival", {}))
            extra = extra.get("custom", {})
        custom = {**PLAYER_NAMES, **RIVAL_NAMES, **extra}
        self.custom = custom
        for k, ko in custom.items():
            kind = "player" if k in PLAYER_NAMES else "rival" if k in RIVAL_NAMES else "custom"
            self.gloss[k] = {"ko": ko, "kind": kind}
        for k, ko in EXTRA_NAMES.items():
            self.gloss.setdefault(k, {"ko": ko, "kind": "name"})
        for k, ko in TRAINER_CLASSES.items():
            if " " not in k:
                self.gloss.setdefault(k, {"ko": ko, "kind": "trainer"})
        for k, (_, ko) in VOCAB.items():
            if KATA_RUN.fullmatch(k):
                self.gloss.setdefault(k, {"ko": ko, "kind": "word"})
        self.by_len = {}
        for name in self.gloss:
            if KATA_RUN.fullmatch(name):
                self.by_len.setdefault(len(name), []).append(name)
        self._tagger = None

    @property
    def tagger(self):
        if self._tagger is None:
            import fugashi
            self._tagger = fugashi.Tagger()
        return self._tagger

    def _best(self, run):
        if run in self.gloss:
            return run, 0.0, 9.0
        cands = []
        for L in (len(run) - 1, len(run), len(run) + 1):
            for name in self.by_len.get(L, ()):
                cands.append((wdist(run, name), name))
        if not cands:
            return None, 9.0, 9.0
        cands.sort()
        second = cands[1][0] if len(cands) > 1 else 9.0
        return cands[0][1], cands[0][0], second

    def correct_and_hint(self, text):
        """-> (corrected_text, hint_block). Only confident, unambiguous fixes are applied."""
        fixes, names = [], []
        for run in dict.fromkeys(KATA_RUN.findall(text)):
            best, cost, second = self._best(run)
            limit = 0.6 if len(run) >= 4 else 0.3 if len(run) == 3 else 0.0   # only cheap slips, never a full swap
            if best is None or cost > limit or second < cost + 0.3:
                continue
            if cost > 0:
                fixes.append((run, best))
            if best in self.gloss:
                g = self.gloss[best]
                if g["kind"] != "word":
                    names.append(f"{best}({g['ko']}, {KIND[g['kind']]})")
        corrected = text
        for a, b in fixes:
            corrected = corrected.replace(a, b)
        chunks = corrected.split()
        bare = {c.rstrip("！？。、…") for c in chunks}
        bare |= {c[:-1] for c in bare if c and c[-1] in PARTICLES}
        covered = [b for _, b in fixes] + [n.split("(")[0] for n in names]
        for name, g in self.gloss.items():   # names that aren't pure katakana (キズぐすり, ひのこ…)
            if KATA_RUN.fullmatch(name) or name in covered or len(name) < 3:
                continue
            if name in bare or (len(name) >= 4 and re.search(r"[ァ-ヴ]", name) and name in corrected) \
                    or (name in self.custom and name in corrected):
                names.append(f"{name}({g['ko']}, {KIND[g['kind']]})")
                covered.append(name)

        words = []
        for kana, (ja, ko) in VOCAB.items():
            if len(kana) <= 2:            # short words: whole chunk (+1 particle) only
                hit = any(c.rstrip("！？。、…") in (kana, *(kana + p for p in PARTICLES)) for c in chunks)
            else:
                hit = kana in corrected
            if hit and not KATA_RUN.fullmatch(kana):
                words.append(f"{kana}={ja}({ko})")
                covered.append(kana)

        unsure = []
        for w in self.tagger(corrected):
            f, s = w.feature, w.surface
            lemma = (f.lemma or "").split("-")[0]
            if len(s) < 2 or not re.search(r"[一-龥]", lemma) or any(s in c for c in covered) \
                    or lemma in ("為る", "成る", "居る", "有る", "出来る"):     # too common to be worth a hint
                continue
            if f.pos1 in ("動詞", "形容詞") or (f.pos1 == "名詞" and len(s) >= 3):
                unsure.append(f"{s}→{lemma}")

        lines = []
        if fixes:
            lines.append("[후보] " + " / ".join(f"{a}→{b}" for a, b in fixes))
        if names:
            lines.append("[이름] " + " / ".join(names))
        if words:
            lines.append("[단어] " + " / ".join(words))
        if unsure:
            lines.append("[참고] (자동 분석이라 틀릴 수 있음) " + " / ".join(dict.fromkeys(unsure)))
        return corrected, "\n".join(lines)

    def lexicon_spans(self, text):
        """Katakana runs that confidently match a dictionary name of the same length -> [(start, name)].
        Used to teach the glyph OCR (each char of the run maps 1:1 to a glyph)."""
        out = []
        for m in KATA_RUN.finditer(text):
            run = m.group()
            if len(run) < 3:
                continue
            best, cost, second = self._best(run)
            limit = 0.6 if len(run) >= 4 else 0.3
            if best is None or len(best) != len(run) or cost > limit or second < cost + 0.3:
                continue
            out.append((m.start(), best))
        return out

    # ------------------------------------------------------------ pronunciation
    def to_hangul(self, text):
        """Particles は/へ are pronounced わ/え; convert the whole line at once so っ/ん attach correctly."""
        parts = []
        for w in self.tagger(text):
            s = w.surface
            if s in ("は", "へ") and w.feature.pos1 == "助詞":
                s = "わ" if s == "は" else "え"
            parts.append(w.white_space + s)
        return kana_to_hangul("".join(parts))


# ---------------------------------------------------------------- kana -> hangul
_BASE = dict(zip(
    "あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわをが"
    "ぎぐげござじずぜぞだぢづでどばびぶべぼぱぴぷぺぽぁぃぅぇぉゃゅょゎゔ",
    "아이우에오카키쿠케코사시스세소타치츠테토나니누네노하히후헤호마미무메모야유요라리루레로와오가"
    "기구게고자지즈제조다지즈데도바비부베보파피푸페포아이우에오야유요와부"))
_YOON = {}
for k, row in {"き": "캬큐쿄", "し": "샤슈쇼", "ち": "차추초", "に": "냐뉴뇨", "ひ": "햐휴효", "み": "먀뮤묘",
               "り": "랴류료", "ぎ": "갸규교", "じ": "자주조", "ぢ": "자주조", "び": "뱌뷰뵤", "ぴ": "퍄퓨표"}.items():
    for small, h in zip("ゃゅょ", row):
        _YOON[k + small] = h
_YOON.update({"ふぁ": "파", "ふぃ": "피", "ふぇ": "페", "ふぉ": "포", "てぃ": "티", "でぃ": "디",
              "とぅ": "투", "どぅ": "두", "うぃ": "위", "うぇ": "웨", "うぉ": "워", "しぇ": "셰",
              "じぇ": "제", "ちぇ": "체", "ゔぁ": "바", "ゔぃ": "비", "ゔぇ": "베", "ゔぉ": "보",
              "いぇ": "예", "でゅ": "듀", "てゅ": "튜", "ふゅ": "퓨"})
_PUNCT = {"！": "!", "？": "?", "、": ",", "。": ".", "…": "…", "　": " ", "ー": "-"}


def _add_final(out, t):
    """t: 4=ㄴ, 19=ㅅ (jongseong index)."""
    if out:
        c = ord(out[-1]) - 0xAC00
        if 0 <= c < 11172 and c % 28 == 0:
            out[-1] = chr(ord(out[-1]) + t)
            return
    out.append("ㄴ" if t == 4 else "ㅅ")


# ー stretches the previous vowel: リー→리이, ドー→도오, ショー→쇼오 (so グリーン→구리인)
_LONG = {0: "아", 2: "아", 9: "아", 4: "어", 14: "어", 5: "에", 7: "에", 15: "에",
         8: "오", 12: "오", 13: "우", 17: "우", 18: "우", 20: "이", 16: "이"}


def _long_vowel(out):
    if out:
        c = ord(out[-1]) - 0xAC00
        if 0 <= c < 11172 and c % 28 == 0:
            return _LONG.get((c // 28) % 21, "-")
    return "-"


def kana_to_hangul(text):
    s = "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in text)   # kata -> hira
    out, i = [], 0
    while i < len(s):
        two = s[i:i + 2]
        if two in _YOON:
            out.append(_YOON[two]); i += 2; continue
        c = s[i]
        if c == "っ":
            _add_final(out, 19)
        elif c == "ん":
            _add_final(out, 4)
        elif c == "ー":
            out.append(_long_vowel(out))
        elif c in _BASE:
            out.append(_BASE[c])
        else:
            out.append(_PUNCT.get(c, c))
        i += 1
    return "".join(out)


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "build":
        build()
    elif len(sys.argv) >= 3 and sys.argv[1] == "check":
        h = Hinter()
        for t in sys.argv[2:]:
            fixed, hints = h.correct_and_hint(t)
            print(f"\nOCR : {t}\n교정: {fixed}\n발음: {h.to_hangul(fixed)}\n{hints}")
    else:
        print(__doc__)
