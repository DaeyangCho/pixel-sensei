#!/usr/bin/env python3
"""
Photos from your own play for the vocab page:
  - places : the area-name popup (top-left box when you enter a town / cave) -> data/snaps/places/<name>.png
  - people : "<gym leader>が しょうぶを しかけてきた！" -> the battle screen with their picture -> data/snaps/people/<name>.png
Each name is saved once (the first good shot); delete the file to retake it.
Frames are stored at native GBA size (240x160).
"""
import hashlib
import re
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
SNAPS = HERE / "data" / "snaps"
DEBUG = HERE / "debug" / "popup"

PLACES = ["マサラタウン", "トキワシティ", "ニビシティ", "ハナダシティ", "クチバシティ", "シオンタウン", "タマムシシティ",
          "ヤマブキシティ", "セキチクシティ", "グレンタウン", "セキエイこうげん", "トキワのもり", "おつきみやま",
          "ディグダのあな", "イワヤマトンネル", "ハナダのどうくつ", "ふたごじま", "チャンピオンロード", "ポケモンタワー",
          "むじんはつでんしょ", "ポケモンやしき", "サファリゾーン", "サントアンヌごう", "シルフカンパニー",
          "ロケットゲームコーナー", "タマムシデパート", "サイクリングロード", "ナナシマ"]
PEOPLE = ["タケシ", "カスミ", "マチス", "エリカ", "キョウ", "ナツメ", "カツラ", "サカキ", "カンナ", "シバ", "キクコ",
          "ワタル", "グリーン"]

POP = (0, 120, 0, 30)             # native x0, x1, y0, y1 searched for the popup box
STABLE = 6                        # frames the popup must stay still before reading it


def _kana(s):
    return "".join(c for c in s if "ぁ" <= c <= "ゖ" or "ァ" <= c <= "ヺ" or c == "ー")


class Snapper:
    def __init__(self, scale, run_ocr):
        self.scale, self.run_ocr = scale, run_ocr
        self.last_hash, self.stable, self.tried = None, 0, set()
        self.read = {}                            # popup image hash -> what it said (each popup is read once)
        self.n_debug = 0
        for sub in ("places", "people"):
            (SNAPS / sub).mkdir(parents=True, exist_ok=True)

    @staticmethod
    def have(sub, name):
        return (SNAPS / sub / f"{name}.png").exists()

    def _save(self, sub, name, native):
        from PIL import Image
        Image.fromarray(np.ascontiguousarray(native)).save(SNAPS / sub / f"{name}.png")
        print(f"\n[사진] {name} 저장 ({sub})", flush=True)

    # ------------------------------------------------------------ people (from dialog text)
    def on_dialog(self, text, native):
        if "しょうぶ" not in text:
            return
        for name in PEOPLE:
            if name in text and not self.have("people", name):
                self._save("people", name, native)

    # ------------------------------------------------------------ places (popup box)
    def on_frame(self, native, raw_full):
        """-> the area name, once each time the popup appears (for the panel), else None.
        The first good shot of a known place is also saved as its photo."""
        x0, x1, y0, y1 = POP
        reg = native[y0:y1, x0:x1].astype(int)
        flat = reg.reshape(-1, 3)
        q = flat // 16
        keys, counts = np.unique(q, axis=0, return_counts=True)
        bg = keys[counts.argmax()] * 16 + 8
        frac = counts.max() / len(flat)
        lum = reg.mean(2)
        text = (lum < 110).sum()
        if not (frac > 0.45 and bg.min() > 150 and text >= 25):
            self.last_hash, self.stable = None, 0
            return
        h = hashlib.md5(np.packbits(lum < 110).tobytes()).hexdigest()
        if h != self.last_hash:
            self.last_hash, self.stable = h, 0
            return
        self.stable += 1
        if self.stable != STABLE:
            return None
        if h in self.read:                             # seen this popup before: no second OCR
            got = self.read[h]
            name = self.match(got)
            return name or (got if len(got) >= 3 else None)
        s = self.scale
        crop = raw_full[int(y0 * s):int(y1 * s), int(x0 * s):int(x1 * s)]
        from PIL import Image
        img = Image.fromarray(np.ascontiguousarray(crop)).convert("L")
        try:
            from place_names import clean
            got = clean(self.run_ocr(img))            # kana + digits (1ばんどうろ)
        except Exception:
            return None
        self.read[h] = got
        name = self.match(got)
        if self.n_debug < 40:                          # a few samples to tune the box, kept in debug/popup
            DEBUG.mkdir(parents=True, exist_ok=True)
            img.save(DEBUG / f"{time.strftime('%H%M%S')}_{got or 'none'}_{name or 'x'}.png")
            self.n_debug += 1
        if name and not self.have("places", name):
            self._save("places", name, native)
        return name or (got if len(got) >= 3 else None)

    @staticmethod
    def match(got):
        if len(got) < 3:
            return None
        if got in PLACES:
            return got
        from jp_text import wdist
        scored = sorted((wdist(got, p), p) for p in PLACES if abs(len(p) - len(got)) <= 2)
        if scored and scored[0][0] <= max(0.6, 0.15 * len(scored[0][1])) and \
                (len(scored) < 2 or scored[1][0] >= scored[0][0] + 0.5):
            return scored[0][1]
        return None
