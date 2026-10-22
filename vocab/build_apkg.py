"""FireRed Japanese vocab page (vocab/fr-vocab.html data) + game lexicon -> AnkiDroid deck (.apkg).

usage:  pip install genanki && python vocab/build_apkg.py      ->  vocab/anki/FR_japanese_v2.apkg

Stable ids: model / deck ids are constants and every note's GUID comes from its deck + key, so importing
a newer build updates the same notes (study progress kept) instead of adding duplicates.
"""
import base64
import hashlib
import html
import json
import re
import sys
from pathlib import Path

import genanki

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from jp_text import Hinter, kana_to_hangul  # noqa: E402
import vocab_live  # noqa: E402

OUT = ROOT / "vocab" / "anki"
OUT.mkdir(exist_ok=True)
MEDIA = OUT / "media"
MEDIA.mkdir(exist_ok=True)
PARENT = "FR 일본어 v2"
H = Hinter()

src = open(ROOT / "vocab" / "fr-vocab.html", encoding="utf-8").read()
D = json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', src, re.S)
               .group(1).replace("<\\/", "</"))
LEX = vocab_live.Vocab(H.gloss, progress_path=str(ROOT / "data" / "progress.json"),
                       src=str(ROOT / "lexicon_src.txt"))

CSS = """
.card{font-family:"Hiragino Sans","Noto Sans CJK JP","Apple SD Gothic Neo",sans-serif;font-size:19px;text-align:center;
      line-height:1.5;padding:12px 10px;color:#1f2a33;background:#f6f8f9}
.night_mode .card, .card.nightMode{color:#e7eef2;background:#141a20}
.jp{font-size:40px;line-height:1.3;word-break:keep-all}
.jp.s{font-size:28px}.jp.xl{font-size:96px}
.img img{max-width:160px;image-rendering:pixelated;margin:6px auto;display:block}
.img.icon img{max-width:56px}
.hint{font-size:14px;color:#8a97a0;margin-top:6px}
hr#answer{margin:14px 0;border:0;border-top:1px dashed #9aabb6}
.pr{color:#cf2f27;font-size:20px}
.night_mode .pr{color:#ff7a70}
.ko{font-size:24px;font-weight:700;margin-top:4px}
.kanji{font-size:22px;color:#5f6f7a;margin-top:2px}
.badge{display:inline-block;font-size:13px;border-radius:6px;padding:1px 8px;margin:6px 3px 0;background:#e3ebef;color:#2f8f5b}
.night_mode .badge{background:#22303a;color:#5cc48a}
.note{font-size:16px;text-align:left;background:#fbe3e1;border-radius:8px;padding:8px 10px;margin-top:10px}
.night_mode .note{background:#3a1f1e}
.ex{text-align:left;margin-top:10px;border-top:1px dashed #9aabb6;padding-top:8px}
.ex .j{font-size:18px}.ex .p{font-size:14px;color:#cf2f27}.ex .k{font-size:15px;color:#5f6f7a}
.parts{display:flex;flex-wrap:wrap;gap:6px;justify-content:center;margin-top:10px}
.parts span{font-size:14px;background:#e3ebef;border-radius:6px;padding:3px 8px}
.night_mode .parts span{background:#22303a}
.parts b{font-size:17px;margin-right:4px}
.types span{display:inline-block;color:#fff;border-radius:5px;padding:1px 8px;margin:2px;font-size:14px}
.swatch{height:56px;border-radius:8px;margin:8px auto;max-width:260px;box-shadow:inset 0 0 0 1px rgba(0,0,0,.15)}
"""
MODEL = genanki.Model(
    1765432101, "FR 일본어 카드",
    fields=[{"name": n} for n in ("Key", "Front", "Image", "Speak", "Reading", "Meaning", "Details")],
    templates=[{
        "name": "읽기",
        "qfmt": '<div class="img">{{Image}}</div><div class="jp">{{Front}}</div>{{tts ja_JP:Speak}}',
        "afmt": '{{FrontSide}}<hr id="answer"><div class="pr">{{Reading}}</div><div class="ko">{{Meaning}}</div>'
                '<div>{{Details}}</div>',
    }],
    css=CSS, sort_field_index=1)

DECKS = {}


def deck(n, name):
    key = f"{PARENT}::{n} {name}"
    if key not in DECKS:
        DECKS[key] = genanki.Deck(int(hashlib.md5(key.encode()).hexdigest()[:8], 16) + 1_000_000_000, key)
    return DECKS[key]


media_files = []


def media(data_uri, name):
    if not data_uri or not data_uri.startswith("data:image"):
        return ""
    raw = base64.b64decode(data_uri.split(",", 1)[1])
    p = MEDIA / name
    p.write_bytes(raw)
    media_files.append(str(p))
    return f'<img src="{name}">'


e = html.escape


def ex_html(ja, pr, ko):
    if not ja:
        return ""
    return (f'<div class="ex"><div class="j">{e(ja)}</div>' + (f'<div class="p">{e(pr)}</div>' if pr else "")
            + (f'<div class="k">{e(ko)}</div>' if ko else "") + "</div>")


def add(dk, key, front, meaning, reading="", details="", image="", speak=None, cls=""):
    front_html = f'<span class="{cls}">{e(front)}</span>' if cls else e(front)
    note = genanki.Note(model=MODEL, guid=genanki.guid_for("frjp-v2", dk.name.split("::")[-1], str(key)),
                        fields=[str(x) for x in (key, front_html, image, speak if speak is not None else front, reading, meaning, details)])
    dk.add_note(note)


# 1·2 kana
for r in D["kana"]:
    for n, name, ch, ex in ((1, "히라가나", r["h"], r["exh"]), (2, "가타카나", r["k"], r["exk"])):
        det = f'<div class="hint">{e(r["r"])} · {e(r["g"])}</div>' + (ex_html(ex["ja"], ex["pr"], ex["ko"]) if ex else "")
        add(deck(n, name), ch, ch, r["ko"], "", det, cls="jp xl")

# 3 confusing pairs
kana_ko = {}
for r in D["kana"]:
    kana_ko[r["h"]] = kana_ko[r["k"]] = r["ko"]
for c in D["confuse"]:
    add(deck(3, "헷갈리는 가나"), c["a"] + c["b"], f'{c["a"]}  {c["b"]}', f'{c["a"]} = {kana_ko.get(c["a"], "?")}  ·  {c["b"]} = {kana_ko.get(c["b"], "?")}',
        "", f'<div class="note">{e(c["tip"])}</div>', speak=f'{c["a"]}、{c["b"]}')

# 4 battle sentences
for s in D["sentences"]:
    parts = "".join(f"<span><b>{e(p[0])}</b>{e(p[2])}</span>" for p in s["parts"])
    add(deck(4, "전투 문장"), s["ja"], s["ja"], s["ko"], s["pr"],
        f'<div class="hint">{e(s.get("g", ""))}</div><div class="parts">{parts}</div>', cls="jp s")

# 5 core words: game lexicon (by how often you have seen them) + the page's words
page_words = {w["ja"]: w for w in D["words"]}
skip = {t["ja"] for t in D["terms"]} | {p["ja"] for p in D["places"]} | {p["ja"] for p in D["people"]}
rows = []
for ja in LEX.order:
    if ja in skip:
        continue
    L = LEX.lex[ja]
    p = LEX.prog.get(ja, {})
    pw = page_words.get(L["ja"]) or page_words.get(ja)
    ex = (pw["ex"], pw["expr"], pw["exko"]) if pw and pw.get("ex") else None
    if not ex:
        for j, k in p.get("ex", []):
            if "□" not in j and not re.search(r"[A-Za-z]", j):
                ex = (j, H.to_hangul(j), k)
                break
    rows.append({"ja": L["ja"], "kanji": L["kanji"], "ko": L["ko"], "note": (pw or {}).get("note") or L["note"],
                 "kr": L["kr"], "same": L["same"], "seen": p.get("seen", 0), "ex": ex})
have = {r["ja"] for r in rows}
for w in D["words"]:
    if w["ja"] not in have and not all(x in have for x in w["ja"].split("・")):
        rows.append({"ja": w["ja"], "kanji": w["kanji"], "ko": w["ko"], "note": w["note"], "kr": "", "same": False,
                     "seen": w.get("n", 0), "ex": (w["ex"], w["expr"], w["exko"]) if w.get("ex") else None})
rows.sort(key=lambda r: -r["seen"])
for r in rows:
    badges = ""
    if r["kr"]:
        badges = f'<span class="badge">{"한국어와 같음 · " if r["same"] else ""}한자 소리 {e(r["kr"])}</span>'
    det = (f'<div class="kanji">{e(r["kanji"])}</div>' if r["kanji"] else "") + badges \
        + (f'<div class="note">{e(r["note"])}</div>' if r["note"] else "") \
        + (ex_html(*r["ex"]) if r["ex"] else "") + (f'<div class="hint">게임에서 {r["seen"]}번 봄</div>' if r["seen"] else "")
    add(deck(5, "핵심 단어"), r["ja"], r["ja"], r["ko"], wpr := " · ".join(kana_to_hangul(x) for x in r["ja"].split("・")), det)

# 6 moves
for m in D["moves"]:
    det = (f'<div class="kanji">{e(m["kanji"])}</div>' if m.get("kanji") else "") \
        + (f'<div class="note">{e(m["note"])}</div>' if m.get("note") else "")
    add(deck(6, "기술"), m["ja"], m["ja"], m["ko"], m["pr"], det)

# 7 items
for i, it in enumerate(sorted(D["items"], key=lambda x: -int(x.get("n") or 0))):
    img = media(it.get("img"), f"frjp_item_{hashlib.md5(it['ja'].encode()).hexdigest()[:10]}.png")
    det = (f'<div class="kanji">{e(it["kanji"])}</div>' if it.get("kanji") else "") \
        + f'<div class="hint">{e(it.get("g", ""))}</div>' + (f'<div class="note">{e(it["note"])}</div>' if it.get("note") else "")
    add(deck(7, "도구"), it["ja"], it["ja"], it["ko"], it["pr"], det, image=f'<span class="icon">{img}</span>' if img else "")

# 8 places / people / terms
for pl in D["places"]:
    img = media(pl.get("photo"), f"frjp_place_{hashlib.md5(pl['ja'].encode()).hexdigest()[:10]}.png")
    sw = f'<div class="swatch" style="background:{e(pl["sw"])}"></div><div class="hint">{e(pl.get("swname", ""))}</div>' if pl.get("sw") else ""
    det = (f'<div class="kanji">{e(pl["kanji"])}</div>' if pl.get("kanji") else "") + sw \
        + (f'<div class="note">{e(pl["note"])}</div>' if pl.get("note") else "")
    add(deck(8, "지명·인물·용어"), pl["ja"], pl["ja"], pl["ko"], pl["pr"], det, image=img)
for pe in D["people"]:
    det = f'<div class="hint">{e(pe.get("role", ""))}</div>' \
        + (f'<div class="hint">배지: {e(pe["badge"])} ({e(pe.get("kbadge", ""))})</div>' if pe.get("badge") else "") \
        + (f'<div class="note">{e(pe["note"])}</div>' if pe.get("note") else "")
    add(deck(8, "지명·인물·용어"), pe["ja"], pe["ja"], pe["ko"], pe["pr"], det)
for t in D["terms"]:
    det = (f'<div class="kanji">{e(t["kanji"])}</div>' if t.get("kanji") else "") + f'<div class="hint">{e(t.get("g", ""))}</div>' \
        + (f'<div class="note">{e(t["note"])}</div>' if t.get("note") else "")
    add(deck(8, "지명·인물·용어"), "t:" + t["ja"], t["ja"], t["ko"], t["pr"], det)

# 9 pokedex
TYPE_COLOR = {"ノーマル": "#8a8a6c", "ほのお": "#e2572b", "みず": "#4a7fe0", "くさ": "#4f9e3a", "でんき": "#c9a000",
              "こおり": "#3fa7b5", "かくとう": "#b23a2e", "どく": "#9244a0", "じめん": "#b08a3e", "ひこう": "#7a86d6",
              "エスパー": "#e0457b", "むし": "#8a9a1b", "いわ": "#a08a3a", "ゴースト": "#6a5590", "ドラゴン": "#5b3fd6",
              "あく": "#5d4a3e", "はがね": "#7f8a99"}
for d in sorted(D["dex"], key=lambda x: int(x["no"])):
    img = media(d.get("spr"), f"frjp_dex_{int(d['no']):03d}.png")
    types = "".join(f'<span style="background:{TYPE_COLOR.get(t, "#777")}">{e(t)} {e(k)}</span>' for t, k in zip(d["types"], d["ktypes"]))
    det = f'<div class="hint">No.{int(d["no"]):03d} · {e(d["genus"])} ({e(d["kgenus"])})</div><div class="types">{types}</div>' \
        + (f'<div class="note">이름 유래: {e(d["note"])}</div>' if d.get("note") else "") + ex_html(d["ex"], d["expr"], d["exko"])
    add(deck(9, "도감"), d["no"], d["ja"].replace("♀", "♀").replace("♂", "♂"), d["ko"], d["pr"], det, image=img,
        speak=d["ja"].rstrip("♀♂"))

pkg = genanki.Package(list(DECKS.values()))
pkg.media_files = media_files
out = OUT / "FR_japanese_v2.apkg"
pkg.write_to_file(str(out))
for k, dk in DECKS.items():
    print(f"{k}: {len(dk.notes)}")
print("media", len(media_files), "->", out, out.stat().st_size // 1024, "KB")
