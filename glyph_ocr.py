#!/usr/bin/env python3
"""
Bitmap-template OCR for Pokemon FR (Switch, JP) dialog text.

The GBA frame is reconstructed pixel-exact (240x160), so every glyph is an exact bitmap.
Each glyph is cut out, keyed by its pixels, and looked up in a library that fills itself:
  - unknown glyph -> Apple Vision reads that character (aligned to the glyph), shown right away
  - a glyph seen twice with the same safe Vision reading is learned automatically
    (dakuten/handakuten and small-kana are checked against the bitmap, one bitmap per char)
  - everything else waits for you in a labeling page with the dialog box screenshot

  python glyph_ocr.py label                 # labeling page in the browser (http://127.0.0.1:8765)
  python glyph_ocr.py learn a.png b.png     # queue glyphs from grabbed frames
  python glyph_ocr.py read  a.png           # decode with the library (+ Vision fallback)
  python glyph_ocr.py stats
  python glyph_ocr.py prune                 # drop queued glyphs seen only once (they come back if real)
  python glyph_ocr.py label-cli             # terminal labeling (no screenshot)

label input: kana (IME) or romaji: lowercase→ひらがな, UPPERCASE→カタカナ, xtu/xya…→small, '-'→ー
"""
import hashlib
import json
import sys
import threading
import time
import unicodedata
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
DATA = HERE / "data"
LIB = DATA / "glyphs.json"
PENDING = DATA / "glyph_pending.json"
DROP = DATA / "glyph_drop.json"      # bitmaps not worth learning (cut-off / merged pieces): never queued again
CTX = DATA / "glyph_ctx"

MERGE_DIST = 7        # a run starting <= 7 px after the glyph start belongs to the same glyph
MAX_GLYPH_W = 10      # ...unless the merged glyph would be wider than any real glyph (narrow '1' + 'ひ')
SPACE_GAP = 7         # blank columns between glyphs >= 7 -> a space (cell is 10 px)
BAND_UP, BAND_DOWN = 12, 1
AUTO_VOTES = 2        # same Vision reading this many times -> auto-learn (if the bitmap agrees)
SMALL_KANA = set("ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮ")
# look-alikes Vision mixes up: for these a char may own only one bitmap (other chars may have several fonts)
CONFUSABLE = set("シツソンウクワロ口カ力ニ二へヘべベぺペー一1lI|0OoＯ")


# ---------------------------------------------------------------- segmentation
def fg_mask(strip, info, inner):
    x0, x1, y0, y1 = inner
    px = strip[y0:y1, x0:x1].astype(int)
    lum = px.mean(2)
    if info["dark_bg"]:
        fg = lum > 200
    else:
        fg = (lum < 110) | ((px[..., 2] - px[..., 0] > 100) & (lum < 170))   # dark or blue (selected) text
    return fg & ~info["red"]


def lines_of(fg):
    """-> list of (band, bottom_row). Band rows are aligned to each text line's baseline."""
    rows = np.nonzero(fg.any(1))[0]
    groups = []
    for r in rows:
        if groups and r - groups[-1][-1] <= 2:
            groups[-1].append(r)
        else:
            groups.append([r])
    out = []
    for gi, g in enumerate(groups):
        bot = int(g[-1])
        H = BAND_UP + BAND_DOWN + 1
        band = np.zeros((H, fg.shape[1]), bool)
        others = {int(r) for j, o in enumerate(groups) if j != gi for r in o}
        for i in range(H):
            r = bot - BAND_UP + i
            if 0 <= r < fg.shape[0] and r not in others:   # tight line spacing (bag text): keep the
                band[i] = fg[r]                            # neighbour line's pixels out of this band
        out.append((band, bot))
    return out


def segments(band):
    cols = np.nonzero(band.any(0))[0]
    runs = []
    for c in cols:
        if runs and c == runs[-1][1] + 1:
            runs[-1][1] = c
        else:
            runs.append([c, c])
    segs = []
    for a, b in runs:
        if segs and a - segs[-1][0] <= MERGE_DIST and b - segs[-1][0] + 1 <= MAX_GLYPH_W:
            segs[-1][1] = b
        else:
            segs.append([a, b])
    return [(int(a), int(b)) for a, b in segs]


def glyph_key(g):
    return f"{g.shape[1]}:" + hashlib.md5(np.packbits(g).tobytes()).hexdigest()[:16]


def glyph_ascii(g):
    rows = [r for r in range(g.shape[0]) if g[r].any()]
    if not rows:
        return ""
    return "\n".join("    " + "".join("██" if v else "··" for v in g[r]) for r in range(rows[0], rows[-1] + 1))


# ---------------------------------------------------------------- bitmap sanity checks
def _components(g):
    seen, comps = np.zeros_like(g), []
    H, W = g.shape
    for r0 in range(H):
        for c0 in range(W):
            if g[r0, c0] and not seen[r0, c0]:
                stack, pts = [(r0, c0)], []
                seen[r0, c0] = True
                while stack:
                    r, c = stack.pop()
                    pts.append((r, c))
                    for dr in (-1, 0, 1):
                        for dc in (-1, 0, 1):
                            rr, cc = r + dr, c + dc
                            if 0 <= rr < H and 0 <= cc < W and g[rr, cc] and not seen[rr, cc]:
                                seen[rr, cc] = True
                                stack.append((rr, cc))
                rs, cs = [p[0] for p in pts], [p[1] for p in pts]
                comps.append((len(pts), min(rs), max(rs), min(cs), max(cs)))
    return comps


def glyph_props(g, base_top=None):
    """base_top: typical top row of glyphs on this line (fonts differ: dialog vs menus)."""
    rows = np.nonzero(g.any(1))[0]
    top, h, w = int(rows[0]), int(rows[-1] - rows[0] + 1), g.shape[1]
    if base_top is None:
        base_top = 4
    # diacritic glyphs reach above the font's usual top, but only on the right (お/な/む also rise, in the middle)
    ys, xs = np.nonzero(g[:max(base_top, 0)])
    mark = w >= 6 and len(xs) > 0 and int(xs.min()) >= w // 2 + 1
    tiny, side_tick = 0, False
    for size, r0, r1, c0, c1 in _components(g):
        if size <= 3 and c0 >= w - 3 and r1 - top <= 5 and size < g.sum():
            side_tick = True                           # small separate stroke on the right (ぐ's dakuten sits mid-right)
        if r1 - top > 2 or c0 < w - 4 or size >= g.sum():
            continue
        if size <= 3:
            tiny += 1                                  # dakuten = two tiny strokes at the top-right
        elif size <= 8 and r1 - r0 >= 2 and c1 - c0 >= 2:
            mark = True                                # handakuten = small ring at the top-right
    if tiny >= 2:
        mark = True
    small = 3 <= h <= 7 and top - base_top >= 2         # ゃゅょっィ: short and sitting low
    return {"mark": mark, "small": small, "side_tick": side_tick, "ring": has_ring(g, top)}


def has_ring(g, top):
    """handakuten ゜ = 3x3 diamond with a hole, in the top-right corner (ポ vs ボ, ピ vs ビ)."""
    H, W = g.shape
    for r in range(top, min(top + 4, H - 2)):
        for c in range(max(W // 2 - 1, 0), W - 2):
            if g[r, c + 1] and g[r + 1, c] and g[r + 1, c + 2] and g[r + 2, c + 1] and not g[r + 1, c + 1]:
                above = r > 0 and g[r - 1, c + 1]                 # ぼ/ざ: dakuten ticks + body can fake a diamond
                left = c > 0 and g[r + 1, c - 1]
                right = c + 3 < W and g[r + 1, c + 3]
                if not (above or left or right):
                    return True
    return False


def char_mark(c):
    return any(ch in "゙゚" for ch in unicodedata.normalize("NFD", c))


def is_kana(c):
    return "ぁ" <= c <= "ゖ" or "ァ" <= c <= "ヺ"


def has_voiced(c):
    """か→が, は→ば/ぱ, う→ゔ … : a Vision reading of c may be a dropped dakuten."""
    return unicodedata.normalize("NFC", c + "\u3099") != c + "\u3099"


def consistent(c, props):
    if not is_kana(c):
        return True
    if char_mark(c) != props["mark"] or (c in SMALL_KANA) != props["small"]:
        return False
    if not char_mark(c) and has_voiced(c) and props.get("side_tick"):
        return False                                   # could be ぐ read as く: leave it for a human
    if "ring" in props and char_mark(c):
        semi = "゚" in unicodedata.normalize("NFD", c)
        if semi != props["ring"]:
            return False                               # Vision often says ボ for ポ: trust the bitmap
    return True


# ---------------------------------------------------------------- alignment with Vision
def align(tokens, vchars):
    """tokens: known char or None (wildcard). -> list (same length) of aligned Vision char or None, n_bad."""
    n, m = len(tokens), len(vchars)
    D = np.zeros((n + 1, m + 1))
    D[:, 0] = np.arange(n + 1)
    D[0, :] = np.arange(m + 1)
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            t, v = tokens[i - 1], vchars[j - 1]
            sub = 0 if (t is None or t == v) else 1
            D[i, j] = min(D[i - 1, j - 1] + sub, D[i - 1, j] + 1, D[i, j - 1] + 1)
    out, i, j, bad = [None] * n, n, m, 0
    while i > 0 and j > 0:
        t, v = tokens[i - 1], vchars[j - 1]
        sub = 0 if (t is None or t == v) else 1
        if D[i, j] == D[i - 1, j - 1] + sub:
            out[i - 1] = v
            bad += sub
            i, j = i - 1, j - 1
        elif D[i, j] == D[i - 1, j] + 1:
            bad += 1 if t is not None else 0
            i -= 1
        else:
            j -= 1
    return out, bad


FULLWIDTH = str.maketrans("!?.,~:;()", "！？。、〜：；（）")


def norm(s):
    return s.translate(FULLWIDTH)


def lexicon_pick(known, vt, cands):
    """Closed vocabulary (move names) of the right length. known: per-cell char or None; vt: Vision kana.
    -> name only when it clearly wins (Vision slips like は/ば, ▶ or a dropped char don't matter)."""
    from jp_text import wdist
    scored = []
    for name in cands:
        mism = sum(1 for k, c in zip(known, name) if k is not None and k != c)
        if mism > 1:
            continue
        scored.append((mism + (wdist(vt, name) if vt else 0.0), name))
    if not scored:
        return None
    scored.sort()
    s0, name = scored[0]
    s1 = scored[1][0] if len(scored) > 1 else 99.0
    if vt:
        ok = s0 <= max(1.0, 0.3 * len(name)) and s1 - s0 >= 0.5
    else:
        ok = sum(k is not None for k in known) >= len(name) - 1 and s0 == 0 and s1 >= 1
    return name if ok else None


def move_lexicon(gloss):
    """{length: [move names]} from the PokeAPI glossary."""
    lex = {}
    for name, g in gloss.items():
        if g.get("kind") == "move":
            lex.setdefault(len(name), []).append(name)
    return lex


# ---------------------------------------------------------------- library
def _load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (ValueError, OSError):
        return {}


def _mtime(path):
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


class GlyphLib:
    """Glyph library + labeling queue. Thread-safe; save() merges with what another process
    (e.g. a separate label page) wrote meanwhile instead of overwriting it."""

    def __init__(self):
        self.lock = threading.RLock()
        self.lib = _load(LIB)
        self.pending = _load(PENDING)
        self.drop = set(_load(DROP) or [])
        for k in self.drop:
            self.pending.pop(k, None)
        old = [k for k, p in self.pending.items() if "props" not in p]   # entries from the first version
        for k in old:                                                    # re-queued with a screenshot
            self.pending.pop(k)
        self.dirty = bool(old)
        self.auto_log = []
        self.corrector = None        # text -> [(start, dictionary word)], set by fr_study (jp_text.Hinter)
        self.move_lexicon = None
        self._lib_changed, self._dropped = set(), set()
        self._synced = (_mtime(LIB), _mtime(PENDING))
        self._last_check = 0.0

    def _merge_from_disk(self):
        disk_lib, disk_pen = _load(LIB), _load(PENDING)
        for k in self._lib_changed:
            disk_lib[k] = self.lib[k]
        for k, p in self.pending.items():
            if k not in disk_lib:
                disk_pen[k] = p
        self.drop = set(_load(DROP) or []) | self.drop
        for k in list(disk_pen):
            if k in self._dropped or k in disk_lib or k in self.drop:
                disk_pen.pop(k)
        self.lib, self.pending = disk_lib, disk_pen

    def refresh(self):
        """Pick up labels saved by another process (cheap; checks file times at most every 2 s)."""
        now = time.time()
        if now - self._last_check < 2:
            return
        self._last_check = now
        with self.lock:
            if (_mtime(LIB), _mtime(PENDING)) != self._synced:
                self._merge_from_disk()
                self._synced = (_mtime(LIB), _mtime(PENDING))

    def save(self):
        with self.lock:
            DATA.mkdir(exist_ok=True)
            if (_mtime(LIB), _mtime(PENDING)) != self._synced:
                self._merge_from_disk()
            LIB.write_text(json.dumps(self.lib, ensure_ascii=False, indent=0), encoding="utf-8")
            PENDING.write_text(json.dumps(self.pending, ensure_ascii=False, indent=0), encoding="utf-8")
            self._synced = (_mtime(LIB), _mtime(PENDING))
            self._lib_changed.clear()
            self._dropped.clear()
            self.dirty = False

    def set_char(self, key, ch):
        """Label (or relabel) a glyph. ch = "" marks it as noise."""
        with self.lock:
            self.lib[key] = ch
            self._lib_changed.add(key)
            if self.pending.pop(key, None) is not None:
                self._dropped.add(key)
            self.save()

    def pending_list(self):
        with self.lock:
            used = self.used_chars()
            out = []
            for k, p in sorted(self.pending.items(), key=lambda kv: -kv[1]["count"]):
                sug = max(p["votes"].items(), key=lambda kv: kv[1])[0] if p["votes"] else p.get("hint", "")
                out.append({"key": k, "file": k.replace(":", "_"), "count": p["count"], "suggest": sug,
                            "context": p.get("context", ""),
                            "conflict": bool(sug and sug in used and sug in CONFUSABLE)})
            return out

    def learned_list(self):
        with self.lock:
            out = []
            for k, ch in self.lib.items():
                f = k.replace(":", "_")
                if (CTX / f"{f}_glyph.png").exists():
                    out.append({"key": k, "file": f, "char": ch, "box": (CTX / f"{f}_box.png").exists()})
            out.sort(key=lambda it: (it["char"] == "", it["char"]))
            return out

    def used_chars(self):
        return {c for c in self.lib.values() if c}

    def _save_ctx(self, key, g, strip, inner, rect):
        from PIL import Image, ImageDraw
        CTX.mkdir(parents=True, exist_ok=True)
        x0, x1, y0, y1 = inner
        k = 4
        box = Image.fromarray(np.ascontiguousarray(strip[max(y0 - 2, 0):y1 + 2, x0 - 2:x1 + 2]))
        box = box.resize((box.width * k, box.height * k), Image.NEAREST)
        d = ImageDraw.Draw(box)
        ra, rb, rt, rbot = rect
        d.rectangle([(ra + 2) * k - 3, (rt + 2) * k - 3, (rb + 3) * k + 2, (rbot + 3) * k + 2],
                    outline=(255, 40, 40), width=3)
        safe = key.replace(":", "_")
        box.save(CTX / f"{safe}_box.png")
        gi = Image.fromarray(np.where(g, 0, 255).astype(np.uint8)).resize((g.shape[1] * 16, g.shape[0] * 16),
                                                                          Image.NEAREST)
        gi.save(CTX / f"{safe}_glyph.png")

    def queue(self, key, g, suggestion, context, strip=None, inner=None, rect=None, base_top=None, hint=None,
              trusted=False):
        """Records a sighting; auto-learns when votes agree and the bitmap is consistent. -> char|None
        trusted: the suggestion comes from a dictionary match (move list / a corrected Pokemon name), not a raw
        Vision reading -> skips the bitmap checks, which misjudge the menu font (its dakuten sit mid-right)."""
        with self.lock:
            if key in self.drop and key not in self.lib:
                return None
            if trusted:
                return self._queue_trusted(key, g, suggestion, context, strip, inner, rect, base_top)
            return self._queue(key, g, suggestion, context, strip, inner, rect, base_top, hint)

    def _queue_trusted(self, key, g, ch, context, strip, inner, rect, base_top):
        if key in self.lib:
            return self.lib[key]
        self._queue(key, g, None, context, strip, inner, rect, base_top, ch)
        if key in self.lib:                            # learned from earlier Vision votes just now
            return self.lib[key]
        p = self.pending[key]
        tv = p.setdefault("tvotes", {})
        tv[ch] = tv.get(ch, 0) + 1
        p["context"], p["context_ok"] = context, True
        best, n = max(tv.items(), key=lambda kv: kv[1])
        if n >= AUTO_VOTES and n * 3 >= sum(tv.values()) * 2:
            return self._learn(key, best)
        return None

    def _learn(self, key, ch):
        self.lib[key] = ch
        self._lib_changed.add(key)
        self.pending.pop(key, None)
        self._dropped.add(key)
        self.auto_log.append(ch)
        self.dirty = True
        return ch

    def _queue(self, key, g, suggestion, context, strip, inner, rect, base_top, hint):
        if key in self.lib:
            return self.lib[key]
        p = self.pending.get(key)
        if p is None:
            p = self.pending[key] = {"art": glyph_ascii(g), "votes": {}, "context": context, "count": 0,
                                     "props": glyph_props(g, base_top)}
            if strip is not None:
                self._save_ctx(key, g, strip, inner, rect)
        p["count"] += 1
        p["props"] = glyph_props(g, base_top)          # re-check with the current rules every time
        if hint and not p.get("hint"):
            p["hint"] = hint                           # shown on the label page, never auto-learned
        if suggestion:
            p["votes"][suggestion] = p["votes"].get(suggestion, 0) + 1
            if not p.get("context_ok"):
                p["context"], p["context_ok"] = context, True
        self.dirty = True
        if p["votes"]:
            best, n = max(p["votes"].items(), key=lambda kv: kv[1])
            taken = best in CONFUSABLE and best in self.used_chars()
            if n >= AUTO_VOTES and consistent(best, p["props"]) and not taken:
                return self._learn(key, best)
        return None

    def decode(self, strip, info, inner, vision=None, ctx_tag=""):
        """Dialog text. -> (text, n_unknown). vision: callable returning Vision text, called only if needed."""
        self.refresh()
        fg = fg_mask(strip, info, inner)
        parsed = []           # per line: list of " " or dict(key, g, char, rect)
        for band, bot in lines_of(fg):
            items, prev_end = [], None
            for a, b in segments(band):
                if prev_end is not None and a - prev_end - 1 >= SPACE_GAP:
                    items.append(" ")
                items.append(self._item(band, bot, a, b + 1))
                prev_end = b
            parsed.append(items)
        return self._finish(parsed, strip, inner, vision, ctx_tag)

    def decode_grid(self, strip, info, inner, origins, cell=8, max_cells=7, vision_at=None, lexicon=None,
                    ctx_tag=""):
        """Fixed-pitch menus (move list): names start at `origins` (columns inside `inner`), one glyph per cell.
        Each name is read on its own: known glyphs + Vision on that name's crop, snapped to `lexicon`
        ({length: [names]}, e.g. all move names). A confident lexicon match teaches its unknown glyphs.
        vision_at(rect) -> Vision text of rect (strip coords x0, x1, y0, y1).  -> (list of names, n_unknown)"""
        self.refresh()
        x0, _, y0, _ = inner
        fg = fg_mask(strip, info, inner)
        names, unknown_total = [], 0
        for band, bot in lines_of(fg):
            for o in origins:
                items = []
                for k in range(max_cells):
                    a = o + k * cell
                    if a + cell > band.shape[1] or not band[:, a:a + cell].any():
                        break
                    items.append(self._item(band, bot, a, a + cell))
                if not items:
                    continue
                for it in items:
                    it["base"] = 3                     # menu font's usual glyph top
                unknown = sum(1 for it in items if it["char"] is None)
                unknown_total += unknown
                if not unknown:
                    names.append("".join(it["char"] for it in items))
                    continue
                rect = (max(x0 + o - 2, 0), min(x0 + o + cell * len(items) + 2, strip.shape[1]),
                        max(y0 + bot - BAND_UP - 2, 0), min(y0 + bot + BAND_DOWN + 3, strip.shape[0]))
                vt = ""
                if vision_at is not None:
                    vt = "".join(c for c in norm(vision_at(rect)) if is_kana(c) or c == "ー")
                names.append(self._grid_name(items, vt, lexicon or {}, strip, inner, ctx_tag))
        return names, unknown_total

    def _grid_name(self, items, vt, lexicon, strip, inner, ctx_tag):
        n = len(items)
        known = [it["char"] for it in items]
        pick = lexicon_pick(known, vt, lexicon.get(n, ()))
        if pick:
            source, chars, strict = "lexicon", list(pick), True
        elif len(vt) == n:
            source, chars, strict = "vision", list(vt), True   # same 1:1 voting as the dialog box
        else:
            source, chars, strict = "none", [None] * n, False
        ctx = f"{ctx_tag}{vt or '?'}" + (f" → {pick}" if pick else "")
        for it, ch in zip(items, chars):
            if it["char"] is not None:
                continue
            learned = self.queue(it["key"], it["g"], ch if strict else None, ctx, strip, inner, it["rect"],
                                 it["base"], hint=ch, trusted=source == "lexicon")
            it["char"] = learned or ch
        if source == "none" and vt:
            return vt                                  # couldn't map Vision to cells: show it as is
        return "".join(it["char"] or "□" for it in items)

    def _learn_from_words(self, parsed, strip, inner, ctx_tag):
        """A Pokemon/move/item name the dictionary confidently fixes (タウトリオ -> ダグトリオ) tells us what
        its unknown glyphs are: vote them as trusted. Known glyphs are never overridden."""
        for items in parsed:
            chars = [it if it == " " else it["char"] for it in items]
            if any(c is None or len(c) != 1 for c in chars):
                text = "".join(c if c and len(c) == 1 else "□" for c in chars)
            else:
                text = "".join(chars)
            try:
                spans = self.corrector(text)
            except Exception:                          # never let a hint helper break reading
                spans = []
            for start, word in spans:
                for i, ch in enumerate(word):
                    it = items[start + i]
                    if it == " " or not (it["char"] is None or it.get("fallback")) or it["g"].shape[1] > 11:
                        continue
                    learned = self.queue(it["key"], it["g"], ch, f"{ctx_tag}{text} → {word}", strip, inner,
                                         it["rect"], it["base"], hint=ch, trusted=True)
                    it["char"], it["fallback"] = learned or ch, learned is None

    def _item(self, band, bot, a, b):
        g = band[:, a:b]
        rows = np.nonzero(g.any(1))[0]
        top = bot - BAND_UP
        k = glyph_key(g)
        return {"key": k, "g": g, "char": self.lib.get(k), "rect": (a, b - 1, top + int(rows[0]), top + int(rows[-1]))}

    def _finish(self, parsed, strip, inner, vision, ctx_tag, base_top=4):
        for line in parsed:                            # font's usual glyph top (dialog font 4, menu font 3)
            for it in line:
                if it != " ":
                    it["base"] = base_top
        glyphs = [it for line in parsed for it in line if it != " "]
        unknown = sum(1 for it in glyphs if it["char"] is None)
        whole_vision = None
        if unknown and vision is not None:
            vtext = norm(vision())
            vchars = [c for c in vtext if not c.isspace()]
            strict = len(vchars) == len(glyphs)        # 1:1 -> Vision readings may vote (auto-learn)
            if strict:
                got, bad = list(vchars), sum(1 for it, v in zip(glyphs, vchars) if it["char"] not in (None, v))
            else:
                got, bad = align([it["char"] for it in glyphs], vchars)
            known = len(glyphs) - unknown
            trust = bad <= max(1, known // 4)
            ctx = vtext.replace("\n", " / ")
            for it, v in zip(glyphs, got):
                if it["char"] is None:
                    sug = v if trust else None
                    if it["g"].shape[1] > 11:          # merged blob, not a glyph: don't queue it
                        learned = None
                    else:
                        learned = self.queue(it["key"], it["g"], sug if strict else None, f"{ctx_tag}{ctx}",
                                             strip, inner, it["rect"], it["base"], hint=sug)
                    if learned is not None:
                        it["char"] = learned
                    elif sug:
                        it["char"], it["fallback"] = sug, True
            if not trust and unknown > len(glyphs) // 2:
                whole_vision = vtext                   # mostly unknown and alignment failed: use Vision text
        elif unknown:
            for it in glyphs:
                if it["char"] is None and it["g"].shape[1] <= 11:
                    self.queue(it["key"], it["g"], None, ctx_tag, strip, inner, it["rect"], it["base"])

        if whole_vision is not None:
            return whole_vision, unknown
        if self.corrector is not None:
            self._learn_from_words(parsed, strip, inner, ctx_tag)
        text = "\n".join("".join(it if it == " " else (it["char"] if it["char"] is not None else "□")
                                 for it in items) for items in parsed)
        return text, unknown


# ---------------------------------------------------------------- screen layouts
def _purple(p):
    r, g, b = (int(v) for v in p)
    return b > r + 15 and b > g + 20 and 80 < r < 160


def _gold(p):
    r, g, b = (int(v) for v in p)
    return r > 170 and g > 140 and b < 120


def detect_layout(strip):
    """Bottom strip of the battle screen:
      moves  - two purple boxes: move list (left) + PP/type (right)  -> read the 4 move names only
      menu   - '〇〇は どうする？' (left, gold) + command menu (right, purple) -> read the left box only
      bag    - bag screen: item description (bottom) + selected item name in the list
      dialog - everything else (normal text box)"""
    def blue(y0, y1, x0, x1):
        box = strip[y0:y1, x0:x1].astype(int)
        return ((box[..., 0] < 40) & (box[..., 1] > 100) & (box[..., 2] > 170)).mean()
    if blue(22, 46, 2, 238) > 0.6 and blue(2, 12, 42, 237) < 0.3:
        return "help", HELP_INNER                     # start-menu help bar: low blue strip, text from the left edge
    if blue(2, 46, 42, 237) > 0.5:                    # bright blue description box (bag, shop): judged on the
        return "bag", BAG_INNER                       # whole box, text can't hide it
    row = strip[4]
    if _purple(row[60]) and _purple(row[200]) and int(row[143].astype(int).sum()) < 200:
        return "moves", MOVES_INNER
    if _gold(row[60]) and _purple(row[200]) and int(row[137].astype(int).sum()) < 200:
        return "menu", MENU_INNER
    return "dialog", None


MOVES_INNER = (14, 138, 6, 42)       # left box, right of the ▶ cursor
MOVES_ORIGINS = (2, 66)              # name columns inside MOVES_INNER (strip x = 16 and 80), 8 px pitch
MENU_INNER = (11, 134, 8, 40)
HELP_INNER = (2, 238, 11, 45)        # start-menu help text (2 lines in the blue bar at the bottom)
BAG_INNER = (42, 237, 2, 46)         # item description (3 lines), right of the item icon
BAG_CURSOR_X = 89                    # ▶ column in the item list (full-frame native coords)
BAG_NAME_X = (96, 188)               # item name, left of "× n"


def bag_item_inner(native):
    """Selected row of the bag list -> text area (full-frame native coords) or None."""
    col = native[8:108, BAG_CURSOR_X].astype(int).mean(1)
    rows = np.nonzero(col < 130)[0]
    if len(rows) < 4:
        return None
    c = 8 + (int(rows[0]) + int(rows[-1])) // 2
    return (BAG_NAME_X[0], BAG_NAME_X[1], max(c - 9, 0), c + 9)


# ---------------------------------------------------------------- romaji -> kana (label input)
_R = {}
for cons, row in {"": "あいうえお", "k": "かきくけこ", "s": "さしすせそ", "t": "たちつてと", "n": "なにぬねの",
                  "h": "はひふへほ", "m": "まみむめも", "r": "らりるれろ", "g": "がぎぐげご", "z": "ざじずぜぞ",
                  "d": "だぢづでど", "b": "ばびぶべぼ", "p": "ぱぴぷぺぽ", "x": "ぁぃぅぇぉ"}.items():
    for v, k in zip("aiueo", row):
        _R[cons + v] = k
_R.update({"ya": "や", "yu": "ゆ", "yo": "よ", "wa": "わ", "wo": "を", "nn": "ん", "n": "ん",
           "shi": "し", "chi": "ち", "tsu": "つ", "fu": "ふ", "ji": "じ", "vu": "ゔ",
           "xya": "ゃ", "xyu": "ゅ", "xyo": "ょ", "xtu": "っ", "xtsu": "っ", "xwa": "ゎ",
           "-": "ー", "...": "…", "!": "！", "?": "？", ".": "。", ",": "、"})


def romaji(s):
    s = s.strip()
    if not s:
        return None
    if any(ord(c) > 0x2FFF for c in s):              # typed with a Japanese IME
        return s
    k = _R.get(s.lower())
    if k is None:
        return s if len(s) == 1 else None             # digits, symbols
    if s.isupper() and "ぁ" <= k <= "ゖ":
        k = chr(ord(k) + 0x60)
    return k


# ---------------------------------------------------------------- labeling web page
PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>글자 등록</title>
<style>
body{font-family:-apple-system,sans-serif;background:#f4f3ef;margin:0;padding:20px;color:#222}
h1{font-size:18px;font-weight:600;margin:0 0 6px}.hint{color:#666;font-size:13px;margin-bottom:14px}
.tabs{display:flex;gap:6px;margin:10px 0 14px}.tabs button{font-size:14px;padding:6px 12px;border-radius:8px;
 border:1px solid #ccc;background:#fff;cursor:pointer}.tabs button.on{background:#222;color:#fff;border-color:#222}
.card{background:#fff;border-radius:10px;padding:14px;margin-bottom:14px;display:flex;gap:16px;align-items:center}
.card.done{opacity:.35}.box{max-width:640px;width:100%;image-rendering:pixelated;border-radius:6px}
.glyph{height:120px;image-rendering:pixelated;border:1px solid #ddd}
input{font-size:28px;width:90px;text-align:center;padding:4px}
button{font-size:13px;margin-top:6px}.meta{font-size:12px;color:#888;margin-top:6px}.warn{color:#c33}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(96px,1fr));gap:10px}
.cell{background:#fff;border-radius:10px;padding:8px;text-align:center}.cell img{height:64px;image-rendering:pixelated}
.cell input{font-size:22px;width:60px}.cell.saved{outline:2px solid #3a9}
#q{font-size:15px;width:160px;padding:4px 8px}
</style>
<h1>글자 사전</h1>
<div class=tabs><button id=t1 class=on>새 글자 <span id=n></span></button><button id=t2>등록된 글자 <span id=m></span></button></div>
<div id=hint1 class=hint>빨간 상자 안의 글자를 입력하고 Enter. 추천이 맞으면 그냥 Enter.
로마자 가능: 소문자→ひらがな, 대문자→カタカナ (gu→ぐ, GU→グ, xtu→っ, - →ー). 글자가 아니면 "삭제".</div>
<div id=hint2 class=hint style="display:none">잘못 등록된 글자를 고치는 곳. 글자를 바꾸고 Enter (빈칸 = 무시할 조각).
찾기: <input id=q placeholder="글자로 찾기"></div>
<div id=list></div>
<script>
const B = '__BASE__';
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function post(body){ return (await fetch(B + '/label', {method:'POST', body: JSON.stringify(body)})).json(); }
async function pending(){
  const items = await (await fetch(B + '/pending')).json();
  document.getElementById('n').textContent = '(' + items.length + ')';
  const list = document.getElementById('list'); list.innerHTML = '';
  items.forEach((it, i) => {
    const d = document.createElement('div'); d.className = 'card';
    d.innerHTML = `<img class=glyph src="${B}/img/${it.file}_glyph.png">
      <div><img class=box src="${B}/img/${it.file}_box.png">
      <div class=meta>${it.count}번 봄 · Vision: ${esc(it.context) || '-'}</div>
      ${it.conflict ? `<div class="meta warn">'${esc(it.suggest)}'은 헷갈리기 쉬운 글자예요. 화면을 한 번 더 확인해 주세요.</div>` : ''}</div>
      <div><input value="${esc(it.suggest)}"><br><button>삭제</button></div>`;
    const inp = d.querySelector('input');
    const send = async (del) => {
      const j = await post({key: it.key, char: inp.value, del});
      if (j.ok) { d.classList.add('done'); inp.value = j.char || '삭제'; inp.disabled = true;
                  const nx = list.querySelectorAll('input:not([disabled])')[0]; if (nx) nx.focus(); }
      else alert('못 알아들었어요: ' + inp.value);
    };
    inp.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.isComposing) send(false); });
    d.querySelector('button').onclick = () => send(true);
    list.appendChild(d);
    if (i === 0) setTimeout(() => inp.focus(), 50);
  });
  if (!items.length) list.innerHTML = '<p>등록할 글자가 없어요 🎉</p>';
}
let learned = [];
async function loadLearned(){
  learned = await (await fetch(B + '/learned')).json();
  document.getElementById('m').textContent = '(' + learned.length + ')';
  renderLearned();
}
function renderLearned(){
  const q = document.getElementById('q').value.trim();
  const list = document.getElementById('list'); list.innerHTML = '<div class=grid></div>';
  const grid = list.firstChild;
  learned.filter(it => !q || it.char === q).forEach(it => {
    const d = document.createElement('div'); d.className = 'cell';
    d.innerHTML = `<img src="${B}/img/${it.file}_glyph.png" title="${it.box ? '처음 본 화면 보기' : ''}"><br>
      <input value="${esc(it.char)}">`;
    if (it.box) d.querySelector('img').onclick = () => window.open(`${B}/img/${it.file}_box.png`);
    const inp = d.querySelector('input');
    inp.addEventListener('keydown', async e => {
      if (e.key !== 'Enter' || e.isComposing) return;
      const j = await post({key: it.key, char: inp.value, del: inp.value.trim() === ''});
      if (j.ok) { it.char = j.char; inp.value = j.char; d.classList.add('saved'); } else alert('못 알아들었어요');
    });
    grid.appendChild(d);
  });
}
document.getElementById('q').addEventListener('input', renderLearned);
function tab(which){
  document.getElementById('t1').classList.toggle('on', which === 1);
  document.getElementById('t2').classList.toggle('on', which === 2);
  document.getElementById('hint1').style.display = which === 1 ? '' : 'none';
  document.getElementById('hint2').style.display = which === 2 ? '' : 'none';
  which === 1 ? pending() : loadLearned();
}
document.getElementById('t1').onclick = () => tab(1);
document.getElementById('t2').onclick = () => tab(2);
pending(); fetch(B + '/learned').then(r => r.json()).then(l => document.getElementById('m').textContent = '(' + l.length + ')');
</script>"""


def web_get(g, sub):
    """Shared GET handler for the label page. sub: path below the page's base. -> (code, body, ctype)"""
    if sub in ("", "/"):
        return 200, None, "text/html; charset=utf-8"
    if sub == "/pending":
        return 200, json.dumps(g.pending_list(), ensure_ascii=False).encode(), "application/json"
    if sub == "/learned":
        return 200, json.dumps(g.learned_list(), ensure_ascii=False).encode(), "application/json"
    if sub.startswith("/img/"):
        f = CTX / Path(sub[5:]).name
        if f.exists():
            return 200, f.read_bytes(), "image/png"
    return 404, b"{}", "application/json"


def web_label(g, req):
    key = req.get("key", "")
    if key not in g.pending and key not in g.lib:
        return {"ok": False}
    ch = "" if req.get("del") else romaji(req.get("char", ""))
    if ch is None:
        return {"ok": False}
    g.set_char(key, ch)
    print(f"  글자 {ch or '(삭제)'} 등록 · 남은 글자 {len(g.pending)}", flush=True)
    return {"ok": True, "char": ch}


def page_html(base):
    return PAGE.replace("__BASE__", base).encode("utf-8")


def cmd_label_web(port=8765):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import webbrowser
    g = GlyphLib()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            g.refresh()
            code, body, ctype = web_get(g, self.path)
            self._send(code, page_html("") if body is None else body, ctype)

        def do_POST(self):
            if self.path != "/label":
                return self._send(404, b"{}", "application/json")
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self._send(200, json.dumps(web_label(g, req), ensure_ascii=False).encode(), "application/json")

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    url = f"http://127.0.0.1:{port}"
    print(f"labeling page: {url}  (pending {len(g.pending)}, Ctrl+C to stop)")
    print("tip: while fr_study.py is running, use the 🔤 button in its panel instead")
    webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def cmd_label_cli():
    g = GlyphLib()
    items = sorted(g.pending.items(), key=lambda kv: -kv[1]["count"])
    for n, (key, p) in enumerate(items, 1):
        sug = max(p["votes"].items(), key=lambda kv: kv[1])[0] if p["votes"] else p.get("hint", "")
        print("\n" + "─" * 40 + f"  ({n}/{len(items)}, seen {p['count']}x)\n{p['art']}\n  context : {p['context']}")
        ans = input(f"  char [{sug or '?'}] (s=skip d=delete q=quit) > ").strip()
        if ans == "q":
            break
        if ans == "s":
            continue
        ch = "" if ans == "d" else (sug if ans == "" else romaji(ans))
        if ch is None:
            print("  ?? skipped")
            continue
        g.set_char(key, ch)
    print(f"\nlibrary {len(g.lib)}, pending {len(g.pending)}")


# ---------------------------------------------------------------- CLI
def strip_from_png(path):
    from PIL import Image
    import fr_watch as fw
    full = np.array(Image.open(path).convert("RGB"))
    gba = full[fw.GBA_Y0:fw.GBA_Y0 + int(fw.NATIVE_H * fw.SCALE), fw.GBA_X0:fw.GBA_X0 + int(fw.NATIVE_W * fw.SCALE)]
    strip = fw.to_native(gba, fw.NATIVE_H)[fw.BOX_Y0:fw.BOX_Y1]
    return full, strip, fw.analyze(strip)


def cmd_read(paths):
    import fr_watch as fw
    g = GlyphLib()
    for p in paths:
        full, strip, info = strip_from_png(p)
        if not info["box"]:
            print(f"== {p}: no dialog")
            continue
        t = time.time()
        text, unk = g.decode(strip, info, fw.INNER, ctx_tag=f"[{Path(p).name}] ",
                             vision=lambda: fw.run_ocr(fw.ocr_image_orig(fw.raw_strip_of(full), info)))
        print(f"== {p}  ({(time.time() - t) * 1000:.1f} ms, unknown {unk})\n{text}")
    g.save()
    if g.auto_log:
        print(f"\nauto-learned: {''.join(g.auto_log)}")
    print(f"library {len(g.lib)}, pending {len(g.pending)}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd in ("learn", "read"):
        cmd_read(sys.argv[2:])
    elif cmd == "label":
        cmd_label_web()
    elif cmd == "label-cli":
        cmd_label_cli()
    elif cmd == "prune":
        g = GlyphLib()
        drop = [k for k, p in g.pending.items() if p["count"] < 2]
        for k in drop:
            g.pending.pop(k)
            g._dropped.add(k)
            for suffix in ("_box.png", "_glyph.png"):
                f = CTX / (k.replace(":", "_") + suffix)
                if f.exists():
                    f.unlink()
        g.save()
        print(f"removed {len(drop)} glyphs seen only once; pending {len(g.pending)}")
    elif cmd == "stats":
        g = GlyphLib()
        chars = sorted(g.used_chars())
        print(f"library {len(g.lib)} glyphs ({len(chars)} chars), pending {len(g.pending)}\n{''.join(chars)}")
    else:
        print(__doc__)
