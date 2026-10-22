"""Area-name popup (top-left box when you enter a town / route / cave) -> Korean name.

Kanto names are the official Korean ones. The Sevii Islands (ナナシマ) were never released in Korean,
so those are plain translations (marked "직역").
"""
import re
import unicodedata

PLACES = {
    "マサラタウン": "태초마을", "トキワシティ": "상록시티", "ニビシティ": "회색시티", "ハナダシティ": "블루시티",
    "クチバシティ": "갈색시티", "シオンタウン": "보라타운", "タマムシシティ": "무지개시티", "ヤマブキシティ": "노랑시티",
    "セキチクシティ": "연분홍시티", "グレンタウン": "홍련마을", "セキエイこうげん": "석영고원",
    "トキワのもり": "상록숲", "おつきみやま": "달맞이산", "ディグダのあな": "디그다굴", "イワヤマトンネル": "돌산터널",
    "ハナダのどうくつ": "블루시티 동굴", "ふたごじま": "쌍둥이섬", "チャンピオンロード": "챔피언로드",
    "ポケモンタワー": "포켓몬타워", "むじんはつでんしょ": "무인발전소", "ポケモンやしき": "포켓몬저택",
    "サファリゾーン": "사파리존", "サントアンヌごう": "상트안느호", "サイクリングロード": "사이클링로드",
    "シルフカンパニー": "실프주식회사", "ロケットゲームコーナー": "로켓 게임코너", "タマムシデパート": "무지개시티 백화점",
    "ロケットだんアジト": "로켓단 아지트", "ポケモンリーグ": "포켓몬리그", "オーキドけんきゅうじょ": "오박사 연구소",
    "ポケモンセンター": "포켓몬센터", "フレンドリィショップ": "프렌들리숍", "ポケモンジム": "포켓몬 체육관",
    "クチバのみなと": "갈색시티 항구", "ハナダのみさき": "블루시티 곶", "ポケモンだいすきクラブ": "포켓몬 팬클럽",
    "セキチクジム": "연분홍시티 체육관",
    # Sevii Islands (not released in Korean: plain translations)
    "ナナシマ": "나나섬 (직역: 일곱 섬)", "1のしま": "1의 섬", "2のしま": "2의 섬", "3のしま": "3의 섬",
    "4のしま": "4의 섬", "5のしま": "5의 섬", "6のしま": "6의 섬", "7のしま": "7의 섬",
    "ほてりのみち": "화끈길 (직역: 달아오르는 길)", "ともしびやま": "등불산 (직역)", "ともしびおんせん": "등불 온천 (직역)",
    "きずなばし": "인연 다리 (직역)", "きのみのもり": "나무열매 숲 (직역)", "いてだきのどうくつ": "얼어붙은 동굴 (직역)",
    "ゴージャスリゾート": "고저스 리조트", "かえらずのあな": "돌아올 수 없는 구멍 (직역)", "みずのめいろ": "물의 미로 (직역)",
    "いせきのたに": "유적 계곡 (직역)", "みずのさんぽみち": "물의 산책길 (직역)", "しっぽうけいこく": "칠보 협곡 (직역)",
    "トレーナータワー": "트레이너 타워", "ナナシマのいせき": "나나섬 유적 (직역)", "ふたごじまのどうくつ": "쌍둥이섬 동굴",
    "アスカナいせき": "아스카나 유적", "トレーナーズスクール": "트레이너스쿨",
}

_KANA_DIGIT = str.maketrans("０１２３４５６７８９", "0123456789")


def clean(s):
    s = unicodedata.normalize("NFKC", s or "").translate(_KANA_DIGIT)
    return "".join(c for c in s if c.isdigit() or "ぁ" <= c <= "ゖ" or "ァ" <= c <= "ヺ" or c == "ー")


def korean(name):
    """-> (normalized Japanese name, Korean name or '')."""
    n = clean(name)
    m = re.fullmatch(r"(\d{1,2})ばんどうろ", n)
    if m:
        return n, f"{m.group(1)}번도로"
    m = re.fullmatch(r"(\d{1,2})ばんすいどう", n)
    if m:
        return n, f"{m.group(1)}번수로"
    if n in PLACES:
        return n, PLACES[n]
    from difflib import get_close_matches                        # OCR slip: ハナダシテイ -> ハナダシティ
    best = get_close_matches(n, PLACES.keys(), n=1, cutoff=0.8)
    if best:
        return best[0], PLACES[best[0]]
    m = re.search(r"(\d{1,2})ばん(どうろ|すいどう)", n)
    if m:
        return m.group(0), m.group(1) + ("번도로" if m.group(2) == "どうろ" else "번수로")
    return n, ""
