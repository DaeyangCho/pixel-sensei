"""Drive the Switch from the Mac keyboard through the board, and optionally record a route.

    python drive.py                          # just play / test
    python drive.py --record routes/onsen.txt  # every key you press is saved as a route line

Keys (the terminal window must be focused):
    arrows / WASD : tap = one tile, hold = keep walking      r : running on/off (B held while moving)
    j : A    k : B    u : X    i : Y      b : fight the battle on screen automatically (recorded as "battle")
    c : VS Seeker charge, runs routes/charge.txt (recorded as "include charge.txt")
    Enter : + (START)   \\ or Backspace : - (SELECT, registered item / VS Seeker)
    l / ; : L / R        h : HOME (careful)      q or Ctrl+C : quit (and save the route)
"""
import argparse
import os
import select
import sys
import termios
import time
import tty

from pad import TILE_RUN_MS, TILE_WALK_MS, Pad

ARROWS = {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT"}
WASD = {"w": "UP", "s": "DOWN", "d": "RIGHT", "a": "LEFT"}
BUTTON_KEYS = {"j": "A", "k": "B", "u": "X", "i": "Y", "\r": "START", "\n": "START", "\\": "SELECT", "\x7f": "SELECT",
               "l": "L", ";": "R", "h": "HOME"}


def read_key(fd):
    ch = os.read(fd, 1).decode(errors="ignore")
    if ch == "\x1b":                                     # arrow keys: ESC [ A..D
        seq = os.read(fd, 2).decode(errors="ignore")
        return ARROWS.get(seq[1:], None) if seq.startswith("[") else None
    if ch.lower() in WASD:
        return WASD[ch.lower()]
    return ch


class Recorder:
    def __init__(self, path):
        self.path, self.lines, self.last = path, [], None

    def add(self, line):
        now = time.time()
        if self.last and now - self.last > 1.2 and self.lines:
            self.lines.append(f"wait {min(now - self.last, 8):.1f}")
        self.last = now
        parts = line.split()
        prev = self.lines[-1].split() if self.lines else []
        if parts[0] == "walk" and prev[:2] == parts[:2] and prev[3:] == parts[3:]:   # walk UP 1 + walk UP 2 -> walk UP 3
            self.lines[-1] = " ".join(["walk", parts[1], str(int(prev[2]) + int(parts[2]))] + parts[3:])
        else:
            self.lines.append(line)

    def save(self):
        if not self.path or not self.lines:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("# recorded " + time.strftime("%Y-%m-%d %H:%M") + "\n" + "\n".join(self.lines) + "\n")
        print(f"\r\n저장: {self.path} ({len(self.lines)}줄)\r")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", help="save the keys as a route file")
    ap.add_argument("--port")
    ap.add_argument("--home", action="store_true", help="d-pad instead of the stick (HOME menu)")
    a = ap.parse_args()
    pad = Pad(a.port, stick_dirs=not a.home)
    rec = Recorder(a.record)
    run = False
    print(f"연결됨 {pad.port} · 화살표/WASD 이동(꾹 누르면 계속), r=달리기, j=A k=B, b=자동배틀, c=충전, Enter=START, \\=SELECT, q=종료\r")
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while True:
            k = read_key(fd)
            if k in (None, ""):
                continue
            if k in ("q", "\x03"):
                break
            if k == "r":
                run = not run
                print(f"\r달리기 {'켜짐' if run else '꺼짐'}      ", end="", flush=True)
                continue
            if k in ("UP", "DOWN", "LEFT", "RIGHT"):
                pad.walk(k, 1, run)
                steps = 1
                # key held down -> the terminal repeats it: keep walking until the repeats stop
                if select.select([fd], [], [], 0.05)[0]:
                    pending = []
                    pad.hold(f"{k}+B" if run else k)
                    t0 = time.time()
                    while True:
                        r, _, _ = select.select([fd], [], [], 0.18)
                        if not r:
                            break
                        k2 = read_key(fd)
                        if k2 != k:
                            pending.append(k2)
                            break
                    held = time.time() - t0
                    pad.release()
                    pad.facing = k
                    time.sleep(0.12)
                    tile = (TILE_RUN_MS if run else TILE_WALK_MS) / 1000
                    steps += max(0, round(held / tile))
                rec.add(f"walk {k} {steps}" + (" run" if run else ""))
                print(f"\r{k:<6} {steps}칸{' (달리기)' if run else ''}   ", end="", flush=True)
            elif k == "b":                            # hand the battle to the autopilot, then come back here
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
                try:
                    import auto
                    print("\r\n[자동 배틀] 끝나면 다시 키보드 조종으로 돌아와요 (Ctrl+C: 중단)")
                    auto.Autopilot(pad, auto.Screen()).battle(wait_start=5)
                    auto.Autopilot(pad, auto.Screen()).until_free(15)
                    rec.add("battle")
                except auto.Stop as e:
                    auto.alert(f"멈춤: {e}")
                except KeyboardInterrupt:
                    pad.release()
                    print("\r\n자동 배틀 중단")
                finally:
                    tty.setraw(fd)
                    print("\r키보드 조종으로 돌아옴\r")
            elif k == "c":                            # VS Seeker charge: run routes/charge.txt, then back here
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
                try:
                    import auto
                    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "routes", "charge.txt")
                    print("\r\n[충전] routes/charge.txt 실행 (Ctrl+C: 중단)")
                    auto.run_route(auto.Autopilot(pad, auto.Screen()), path)
                    rec.add("include charge.txt")
                    auto.play(auto.SOUND_DONE)
                except auto.Stop as e:
                    auto.alert(f"멈춤: {e}")
                except KeyboardInterrupt:
                    pad.release()
                    print("\r\n충전 중단")
                finally:
                    tty.setraw(fd)
                    print("\r키보드 조종으로 돌아옴\r")
            elif k in BUTTON_KEYS:
                b = BUTTON_KEYS[k]
                pad.press(b)
                rec.add(f"press {b}")
                print(f"\r{b:<6}", end="", flush=True)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        rec.save()
        pad.close()
        print()


if __name__ == "__main__":
    main()
