"""Live screen state for the autopilot (auto.py), filled by fr_study's capture loop and served at /state.

Unlike the panel cards (sent only when text changes), this is updated every frame, so the autopilot
sees the battle menu again on every turn even when its text is identical.
"""
import re
import threading
import time

LIVE = {"t": 0.0, "seq": 0, "kind": None, "box": False, "battle_box": False, "text_px": 0, "arrow": False,
        "lines": [], "lines_kind": None, "lines_t": 0.0, "hud": [], "hud_t": 0.0}
_hud_busy = threading.Lock()


def on_frame(kind, info, now, native=None):
    LIVE.update(t=now, kind=kind, box=bool(info["box"]), battle_box=bool(info["battle_box"]),
                text_px=int(info["text_px"]), arrow=bool(info["arrow"]))
    if native is not None:                        # screen brightness: ~0 while a door/cave fades to black
        LIVE["lum"] = round(float(native[::4, ::4].mean()), 1)
        if LIVE["lum"] < DARK:
            LIVE["dark_t"] = now


DARK = 25


_pp_busy = threading.Lock()
# move list: the box on the right shows the cursor's move ("PP 14/15", "わざタイプ/でんき")
PP_BOX = (148, 238, 116, 156)                     # native x0, x1, y0, y1


def on_lines(kind, lines, t, raw_full=None, scale=6.5, box_y0=112):
    LIVE.update(seq=LIVE["seq"] + 1, lines=list(lines), lines_kind=kind, lines_t=t)
    if kind == "moves" and raw_full is not None:
        x0, x1, y0, y1 = PP_BOX
        img = raw_full[int(y0 * scale):int(y1 * scale), int(x0 * scale):int(x1 * scale)].copy()
        _pp_next[0] = img                              # always read the latest screen, even if busy now
        if _pp_busy.acquire(blocking=False):
            threading.Thread(target=_read_pp_loop, daemon=True).start()
    if kind == "menu" and raw_full is not None and _hud_busy.acquire(blocking=False):
        # read the HP boxes in the background: names + levels of every Pokemon on the field
        img = raw_full[: int(box_y0 * scale)].copy()
        threading.Thread(target=_read_hud, args=(img,), daemon=True).start()


_LV = re.compile(r"L\s*[vVuU]\s*\.?\s*(\d{1,3})")


def _read_hud(img):
    try:
        from ocrmac import ocrmac
        from PIL import Image
        res = ocrmac.OCR(Image.fromarray(img), language_preference=["ja-JP"], recognition_level="accurate").recognize()
        # bbox = [x, y, w, h] normalized, origin bottom-left
        items = [(t.strip(), b[0], 1 - (b[1] + b[3] / 2)) for t, _, b in res if t.strip()]
        hud = parse_hud(items)
        LIVE.update(hud=hud, hud_t=time.time())
    except Exception as e:  # never break reading for this
        LIVE.update(hud_err=str(e))
    finally:
        _hud_busy.release()


_PP = re.compile(r"(\d{1,2})\s*[/／]\s*(\d{1,2})")
_pp_next = [None]


def _read_pp_loop():
    try:
        while _pp_next[0] is not None:
            img, _pp_next[0] = _pp_next[0], None
            _read_pp(img)
    finally:
        _pp_busy.release()


def _read_pp(img):
    try:
        import numpy as np
        from ocrmac import ocrmac
        from PIL import Image
        # low PP turns the digits yellow / red: make every non-background pixel black on white first
        gray = img.astype(np.int16).mean(axis=2)
        bw = np.where(gray < 200, 0, 255).astype(np.uint8)
        res = ocrmac.OCR(Image.fromarray(bw), language_preference=["ja-JP"], recognition_level="accurate").recognize()
        text = " ".join(t for t, _, _ in res)
        m = _PP.search(text.replace("O", "0").replace("o", "0"))
        mt = re.search(r"タイプ\s*[/／]?\s*(\S+)", text)
        LIVE.update(pp=(int(m.group(1)), int(m.group(2))) if m else None,
                    pp_type=mt.group(1) if mt else None, pp_text=text, pp_t=time.time())
    except Exception as e:
        LIVE.update(pp_err=str(e))


def parse_hud(items):
    """items: (text, x, y) with x, y in 0..1 from the top-left -> [{name, lv, side, x, y}], top to bottom."""
    hud = []
    for t, x, y in sorted(items, key=lambda i: (i[2], i[1])):
        m = _LV.search(t)
        name = _LV.sub("", t)
        for junk in ("♂", "♀", "HP", "EXP"):
            name = name.replace(junk, "")
        name = name.strip(" :：/")
        lv = int(m.group(1)) if m else None
        if not name and lv is not None:               # "Lv39" read as its own word: attach to the name on its row
            near = [h for h in hud if abs(h["y"] - y) < 0.05 and h["lv"] is None]
            if near:
                near[-1]["lv"] = lv
            continue
        if len(name) < 2 or re.fullmatch(r"[\d/ ]+", name):
            continue
        hud.append({"name": name, "lv": lv, "side": "foe" if x < 0.45 else "mine", "x": round(x, 3), "y": round(y, 3)})
    return hud


def snapshot(battle_state=None):
    d = dict(LIVE)
    d["now"] = time.time()
    if battle_state is not None:
        d["battle"] = battle_state
    return d
