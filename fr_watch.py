#!/usr/bin/env python3
"""
Pokemon FireRed (Switch, JP) dialogue watcher - step 1
capture -> native-res reconstruction -> dialog box detect -> "line finished" trigger -> Apple Vision OCR

usage:
  python fr_watch.py calib tests/frames/done1.png            # check GBA area / scale
  python fr_watch.py test  tests/frames/*.png                # offline check on grabbed frames (your own captures)
  python fr_watch.py live  --dev 0                           # real-time from capture card
"""
import argparse
import hashlib
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

# ---- calibration (1920x1080 capture of Switch FRLG) ----
GBA_X0, GBA_Y0, SCALE = 180, 5, 6.5      # GBA 240x160 drawn at 6.5x
NATIVE_W, NATIVE_H = 240, 160
BOX_Y0, BOX_Y1 = 112, 160                # native rows of the bottom dialog strip
INNER = (11, 229, 8, 40)                 # x0, x1, y0, y1 inside the strip (native px)

# ---- trigger tuning ----
FPS = 30
STABLE_WITH_ARROW = 8                    # ~0.27 s unchanged + red arrow
STABLE_NO_ARROW = 20                     # ~0.67 s unchanged, no arrow (auto-advance msgs)
MIN_TEXT_PX = 15

DEBUG = Path("debug")
DEBUG.mkdir(exist_ok=True)


def to_native(img, rows):
    """Sample the centre of each 6.5x6.5 block -> exact native pixels."""
    ys = ((np.arange(rows) + 0.5) * SCALE).astype(int)
    xs = ((np.arange(NATIVE_W) + 0.5) * SCALE).astype(int)
    return img[ys][:, xs]


def analyze(strip, inner=None):
    """strip: (48, 240, 3) uint8 native dialog strip. inner: text area (x0, x1, y0, y1), default INNER."""
    x0, x1, y0, y1 = inner or INNER
    inner = strip[y0:y1, x0:x1].astype(int)
    flat = inner.reshape(-1, 3)

    q = flat // 16
    keys, counts = np.unique(q, axis=0, return_counts=True)
    bg_key = keys[counts.argmax()]
    frac = counts.max() / len(flat)
    bg = flat[(q == bg_key).all(1)].mean(0)
    r, g, b = bg
    dark_bg = b > r + 25 and b > 70          # battle box: dark blue (also the bag's blue box: white text)
    light_bg = min(bg) > 190                 # field box: white
    battle_box = dark_bg and max(bg) < 140   # the real battle text box is dark teal; bag/shop blue and the
                                             # cyan-green town floor are brighter

    red = (inner[..., 0] > 170) & (inner[..., 1] < 90) & (inner[..., 2] < 90)
    arrow = int(red.sum()) >= 4

    text = (np.abs(inner - bg).sum(2) > 90) & ~red
    # a text box is mostly background: a busy map that happens to share one colour is not a box
    box = frac > 0.5 and (dark_bg or light_bg) and text.mean() < 0.3
    mask = text.copy()
    if arrow:                                 # arrow bounces -> hide its area from the hash
        ys, xs = np.nonzero(red)
        mask[max(ys.min() - 4, 0):ys.max() + 5, max(xs.min() - 2, 0):xs.max() + 3] = False
    h = hashlib.md5(np.packbits(mask).tobytes()).hexdigest()

    return {"box": box, "arrow": arrow, "text_px": int(text.sum()), "hash": h,
            "bg": bg.round().astype(int).tolist(), "dark_bg": dark_bg, "battle_box": battle_box, "red": red}


def ocr_image(strip, info, up=8):
    """Binarize main glyphs (drop shadow) -> black text on white, upscaled."""
    x0, x1, y0, y1 = INNER
    inner = strip[y0:y1, x0:x1].astype(int)
    lum = inner.mean(2)
    fg = (lum > 200) if info["dark_bg"] else (lum < 110)
    fg &= ~info["red"]
    img = np.where(fg, 0, 255).astype(np.uint8)
    img = np.pad(img, 4, constant_values=255)
    return Image.fromarray(img).resize((img.shape[1] * up, img.shape[0] * up), Image.NEAREST)


def ocr_image_orig(raw_strip, info, inner=None):
    """Best variant (D): original 1080p pixels of the text area, grayscale, dark-on-light."""
    x0, x1, y0, y1 = inner or INNER
    crop = raw_strip[int(y0 * SCALE):int(y1 * SCALE), int(x0 * SCALE):int(x1 * SCALE)]
    gray = Image.fromarray(np.ascontiguousarray(crop)).convert("L")
    if info["dark_bg"]:
        gray = Image.eval(gray, lambda v: 255 - v)
    return gray


def run_ocr(pil):
    from ocrmac import ocrmac
    res = ocrmac.OCR(pil, language_preference=["ja-JP"], recognition_level="accurate").recognize()
    # bbox = [x, y, w, h] normalized, origin bottom-left -> top line has larger y
    items = sorted(((t, b[0], b[1] + b[3] / 2) for t, _, b in res), key=lambda i: -i[2])
    lines, cur, cur_y = [], [], None
    for t, x, y in items:
        if cur_y is not None and abs(y - cur_y) > 0.15:
            lines.append(cur)
            cur = []
        cur.append((x, t))
        cur_y = y
    if cur:
        lines.append(cur)
    return "\n".join(" ".join(t for _, t in sorted(line)) for line in lines)


def warmup():
    """First Vision call loads the model (~6-7 s). Do it once up front."""
    t = time.time()
    run_ocr(Image.new("L", (200, 60), 255))
    print(f"ocr warm-up {(time.time() - t) * 1000:.0f} ms", flush=True)


def ocr_variants(full, strip, info):
    """Same dialog, different preprocessing -> compare Vision accuracy."""
    from PIL import ImageFilter
    x0, x1, y0, y1 = INNER
    base = ocr_image(strip, info, up=8)
    out = {
        "A_bin_x8": base,
        "B_bin_x8_blur": base.filter(ImageFilter.GaussianBlur(3)),
        "C_bin_x4_blur": ocr_image(strip, info, up=4).filter(ImageFilter.GaussianBlur(1.5)),
    }
    from PIL import ImageOps
    out["D_orig_1080p"] = ocr_image_orig(raw_strip_of(full), info)
    out["E_orig_contrast"] = ImageOps.autocontrast(out["D_orig_1080p"], cutoff=1)
    return out


def raw_strip_of(full):
    """1080p rows/cols of the dialog strip (same region live mode receives from ffmpeg)."""
    y = int(GBA_Y0 + BOX_Y0 * SCALE)
    return full[y:y + int((BOX_Y1 - BOX_Y0) * SCALE), GBA_X0:GBA_X0 + int(NATIVE_W * SCALE)]


# ---------------- modes ----------------
def calib(path):
    a = np.array(Image.open(path).convert("RGB"))
    ys, xs = np.nonzero(a.max(2) > 20)
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    print(f"content bbox: x={x0} y={y0} w={x1 - x0} h={y1 - y0}")
    print(f"scale: {(x1 - x0) / NATIVE_W:.3f} x {(y1 - y0) / NATIVE_H:.3f}")
    print(f"current constants: GBA_X0={GBA_X0} GBA_Y0={GBA_Y0} SCALE={SCALE}")


def variants(paths):
    warmup()
    for p in paths:
        full = np.array(Image.open(p).convert("RGB"))
        gba = full[GBA_Y0:GBA_Y0 + int(NATIVE_H * SCALE), GBA_X0:GBA_X0 + int(NATIVE_W * SCALE)]
        strip = to_native(gba, NATIVE_H)[BOX_Y0:BOX_Y1]
        info = analyze(strip)
        if not (info["box"] and info["text_px"] >= MIN_TEXT_PX):
            continue
        print(f"\n== {p}")
        for name, img in ocr_variants(full, strip, info).items():
            img.save(DEBUG / f"{Path(p).stem}_{name}.png")
            t = time.time()
            text = run_ocr(img).replace("\n", " / ")
            print(f" {name:<14} {(time.time() - t) * 1000:4.0f}ms  {text}")


def test(paths):
    warmup()
    for p in paths:
        full = np.array(Image.open(p).convert("RGB"))
        gba = full[GBA_Y0:GBA_Y0 + int(NATIVE_H * SCALE), GBA_X0:GBA_X0 + int(NATIVE_W * SCALE)]
        native = to_native(gba, NATIVE_H)
        stem = Path(p).stem
        Image.fromarray(native).resize((960, 640), Image.NEAREST).save(DEBUG / f"{stem}_native.png")

        strip = native[BOX_Y0:BOX_Y1]
        info = analyze(strip)
        print(f"\n== {p}\n box={info['box']} arrow={info['arrow']} "
              f"text_px={info['text_px']} bg={info['bg']}")
        if info["box"] and info["text_px"] >= MIN_TEXT_PX:
            img = ocr_image_orig(raw_strip_of(full), info)
            img.save(DEBUG / f"{stem}_ocr_input.png")
            t = time.time()
            text = run_ocr(img)
            print(f" ocr ({(time.time() - t) * 1000:.0f} ms):\n{text}")
        else:
            print(" -> no dialog, skipped")
    print(f"\ndebug images -> {DEBUG.resolve()}")


def live(dev):
    w, h = int(NATIVE_W * SCALE), int((BOX_Y1 - BOX_Y0) * SCALE)
    y = int(GBA_Y0 + BOX_Y0 * SCALE)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
           "-f", "avfoundation", "-framerate", "60", "-video_size", "1920x1080",
           "-pixel_format", "uyvy422", "-i", f"{dev}:none",
           "-vf", f"crop={w}:{h}:{GBA_X0}:{y},fps={FPS}",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    warmup()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    n = w * h * 3
    pool = ThreadPoolExecutor(max_workers=1)

    def job(img, arrow, t0):
        t1 = time.time()
        text = run_ocr(img)
        t2 = time.time()
        ts = time.strftime("%H%M%S")
        img.save(DEBUG / f"live_{ts}.png")
        print(f"[{ts}] {'▼' if arrow else '·'}  ocr {(t2 - t1) * 1000:.0f}ms  "
              f"(trigger→text {(t2 - t0) * 1000:.0f}ms)\n{text}\n", flush=True)

    print("watching... (Ctrl+C to stop)", flush=True)
    last_hash, stable, emitted = None, 0, None
    try:
        while True:
            buf = p.stdout.read(n)
            if len(buf) < n:
                break
            raw = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            strip = to_native(raw, BOX_Y1 - BOX_Y0)
            info = analyze(strip)
            if not info["box"] or info["text_px"] < MIN_TEXT_PX:
                last_hash, stable, emitted = None, 0, None
                continue
            if info["hash"] == last_hash:
                stable += 1
            else:
                last_hash, stable = info["hash"], 0
            need = STABLE_WITH_ARROW if info["arrow"] else STABLE_NO_ARROW
            if stable == need and info["hash"] != emitted:
                emitted = info["hash"]
                pool.submit(job, ocr_image_orig(raw, info), info["arrow"], time.time())
    except KeyboardInterrupt:
        pass
    finally:
        p.terminate()
        pool.shutdown(wait=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)
    sub.add_parser("calib").add_argument("png")
    sub.add_parser("test").add_argument("pngs", nargs="+")
    sub.add_parser("variants").add_argument("pngs", nargs="+")
    sub.add_parser("live").add_argument("--dev", default="0")
    a = ap.parse_args()
    if a.mode == "calib":
        calib(a.png)
    elif a.mode == "test":
        test(a.pngs)
    elif a.mode == "variants":
        variants(a.pngs)
    else:
        live(a.dev)
