#!/usr/bin/env python3
"""Download the battle-view sprites (FireRed/LeafGreen, No.1-386) and the name -> No. table.

Sprites are Nintendo / Game Freak artwork, so they are NOT in this repo. This fetches them from the
PokeAPI sprites project into data/sprites/ (git-ignored) on your own machine.

  python tools/fetch_sprites.py          # ~770 small PNGs, about 1 MB
"""
import csv
import json
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "sprites"
BASE = "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/versions/generation-iii/"
NAMES = "https://raw.githubusercontent.com/PokeAPI/pokeapi/master/data/v2/csv/pokemon_species_names.csv"
LAST = 386


def get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read()


def sprite(args):
    side, no = args
    dst = OUT / side / f"{no}.png"
    if dst.exists():
        return 0
    sub = "back/" if side == "back" else ""
    for game in ("firered-leafgreen", "ruby-sapphire"):        # FRLG first, R/S as a fallback
        try:
            dst.write_bytes(get(f"{BASE}{game}/{sub}{no}.png"))
            return 1
        except Exception:
            continue
    print("missing", side, no, file=sys.stderr)
    return 0


def main():
    for side in ("front", "back"):
        (OUT / side).mkdir(parents=True, exist_ok=True)
    jobs = [(s, n) for s in ("front", "back") for n in range(1, LAST + 1)]
    with ThreadPoolExecutor(16) as ex:
        got = sum(ex.map(sprite, jobs))
    print(f"sprites: {got} downloaded ({len(jobs)} total) -> {OUT}")

    rows = {}
    for r in csv.DictReader(get(NAMES).decode("utf-8").splitlines()):
        i = int(r["pokemon_species_id"])
        if i <= LAST:
            rows.setdefault(i, {})[r["local_language_id"]] = r["name"]
    dex = {}
    for i, v in sorted(rows.items()):
        ja, ko = v.get("1"), v.get("3")                       # 1 = ja-Hrkt (kana), 3 = ko
        if ja and ko:
            dex[ja] = {"id": i, "ko": ko}
            if ja[-1] in "♀♂" and ja[:-1] not in dex:        # ニドラン♀ -> also ニドラン
                dex[ja[:-1]] = {"id": i, "ko": ko}
    (OUT / "pokedex.json").write_text(json.dumps(dex, ensure_ascii=False), encoding="utf-8")
    print(f"pokedex.json: {len(dex)} names")


if __name__ == "__main__":
    main()
