"""Talk to the frpad firmware (ESP32-S3 pretending to be a wired Switch pad).

    from pad import Pad
    p = Pad()                 # finds the board's serial port by itself
    p.press("A")              # tap A (100 ms)
    p.press("UP", 300)        # hold up for 300 ms
    p.walk("RIGHT", 3)        # 3 tiles in the overworld
    p.release()
"""
import glob
import threading
import time

import serial  # pip install pyserial

BUTTONS = {
    "Y": 0x0001, "B": 0x0002, "A": 0x0004, "X": 0x0008, "L": 0x0010, "R": 0x0020, "ZL": 0x0040, "ZR": 0x0080,
    "MINUS": 0x0100, "PLUS": 0x0200, "LSTICK": 0x0400, "RSTICK": 0x0800, "HOME": 0x1000, "CAPTURE": 0x2000,
}
HAT = {"UP": 0, "UPRIGHT": 1, "RIGHT": 2, "DOWNRIGHT": 3, "DOWN": 4, "DOWNLEFT": 5, "LEFT": 6, "UPLEFT": 7}
NEUTRAL_HAT = 8
# Switch FireRed reads this pad's d-pad as buttons (up=START, left=SELECT ...), but the left stick works:
# in the game every direction is sent as a full stick tilt.
STICK = {"UP": (128, 0), "DOWN": (128, 255), "LEFT": (0, 128), "RIGHT": (255, 128),
         "UPLEFT": (0, 0), "UPRIGHT": (255, 0), "DOWNLEFT": (0, 255), "DOWNRIGHT": (255, 255)}
# GBA button -> what to press on this pad in the game (tested 2026-10-17: PLUS opens the menu,
# the d-pad's left is read as SELECT)
GBA = {"START": "PLUS", "SELECT": "HATLEFT"}
HAT_FORCED = {"HATUP": 0, "HATRIGHT": 2, "HATDOWN": 4, "HATLEFT": 6}   # real d-pad even in stick mode

# GBA overworld timing: one tile takes 16 frames walking (~267 ms), 8 frames running (B held)
FRAME_MS = 1000 / 59.73
TILE_WALK_MS = 16 * FRAME_MS
TILE_RUN_MS = 8 * FRAME_MS


def find_port():
    cands = sorted(glob.glob("/dev/cu.usbserial*") + glob.glob("/dev/cu.wchusbserial*") + glob.glob("/dev/cu.usbmodem*")
                   + glob.glob("/dev/cu.SLAB_USBtoUART*") + glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    if not cands:
        raise RuntimeError("보드를 못 찾았어요: COM 포트를 맥에 꽂았는지 확인 (ls /dev/cu.*)")
    return cands[0]


def parse(keys, stick_dirs=False):
    """'A', 'B+UP', ['A', 'LEFT'] -> (button mask, hat, (lx, ly)). With stick_dirs, directions tilt the stick."""
    if isinstance(keys, str):
        keys = [k for k in keys.replace(" ", "").upper().split("+") if k]
    mask, hat, stick = 0, NEUTRAL_HAT, (128, 128)
    for k in keys:
        k = k.upper()
        k = GBA.get(k, k)
        if k in HAT_FORCED:
            hat = HAT_FORCED[k]
        elif stick_dirs and k in STICK:
            stick = STICK[k]
        elif k in BUTTONS:
            mask |= BUTTONS[k]
        elif k in HAT:
            hat = HAT[k]
        else:
            raise ValueError(f"모르는 버튼: {k}")
    return mask, hat, stick


class Pad:
    def __init__(self, port=None, baud=115200, log=None, stick_dirs=True):
        self.stick_dirs = stick_dirs        # True in the game, False for the HOME menu (d-pad works there)
        self.facing = None                  # last walked direction (a turn needs a longer press)
        self.port = port or find_port()
        # open like miniterm does (DTR/RTS asserted together): toggling them one by one pulses EN and
        # restarts the board, and after a restart the Switch does not always re-register it
        try:
            self.ser = serial.Serial(self.port, baud, timeout=0.5)
        except Exception as e:                # termios.error 22: stale port after sleep / replug
            raise RuntimeError(f"{self.port} 를 못 열었어요 ({e}) — 보드 COM 케이블을 맥에서 뺐다 다시 꽂고 "
                               "ls /dev/cu.* 로 포트가 다시 생겼는지 확인하세요") from None
        self.lock = threading.Lock()
        self.log = log                       # optional callable(str): every command, for recording/debug
        v, booted = "", False
        for _ in range(8):                   # if it did reset, it boots in ~0.3 s
            time.sleep(0.25)
            self.ser.write(b"?\n")
            for _ in range(20):
                v = self.ser.readline().decode(errors="replace").strip()
                booted |= "frpad boot" in v
                if v.startswith("OK FRPAD") or not v:
                    break
            if v.startswith("OK FRPAD"):
                break
        else:
            raise RuntimeError(f"frpad 펌웨어 응답 없음 ({self.port}: {v!r}) — 펌웨어를 올렸는지 확인")
        if booted:
            # opening the port restarted the board: wait until the Switch has re-registered it,
            # otherwise the first presses are lost
            t0 = time.time()
            while time.time() - t0 < 4:
                line = self.ser.readline().decode(errors="replace").strip()
                if "usb started" in line:
                    break
            time.sleep(0.5)
        self.version = v

    def cmd(self, s):
        with self.lock:
            self.ser.write((s + "\n").encode())
            resp = ""
            for _ in range(20):               # skip log lines ("# ..." from frpad, "[..." from the core)
                resp = self.ser.readline().decode(errors="replace").strip()
                if resp.startswith(("OK", "ERR")) or not resp:
                    break
        if not resp.startswith("OK"):
            raise RuntimeError(f"보드 응답 이상: {s!r} -> {resp!r}")
        return resp

    # ---- primitives
    def press(self, keys, ms=100, wait=True, gap=60):
        """Press keys for ms (timed on the board), then wait until released + a small gap."""
        mask, hat, (lx, ly) = parse(keys, self.stick_dirs)
        if self.log:
            self.log(f"press {keys} {ms}")
        if (lx, ly) == (128, 128):
            self.cmd(f"P {mask:x} {hat} {int(ms)}")          # timed on the board
            if wait:
                time.sleep((ms + gap) / 1000)
        else:                                                # stick: hold, wait, release (timed here)
            self.cmd(f"S {mask:x} {hat} {lx} {ly} 128 128")
            time.sleep(ms / 1000)
            self.cmd("R")
            if wait:
                time.sleep(gap / 1000)

    def hold(self, keys, lx=128, ly=128, rx=128, ry=128):
        mask, hat, st = parse(keys, self.stick_dirs) if keys else (0, NEUTRAL_HAT, (128, 128))
        if st != (128, 128):
            lx, ly = st
        self.cmd(f"S {mask:x} {hat} {lx} {ly} {rx} {ry}")

    def release(self):
        self.cmd("R")

    def mash(self, keys="A", times=5, ms=60, gap=120):
        for _ in range(times):
            self.press(keys, ms, gap=gap)

    # ---- overworld
    def walk(self, direction, tiles=1, run=False, tile_ms=None):
        """Move whole tiles in one continuous hold (B held = running). Holding a little less than n tiles
        still finishes the n-th one (a started step always completes) and never starts tile n+1.
        A short press in a NEW direction only turns the player, so after a turn the first tile is walked."""
        if tiles <= 0:
            return
        if run and direction != self.facing:
            self.walk(direction, 1, False)            # turn + one sure step
            tiles -= 1
            if tiles <= 0:
                return
        t = tile_ms or (TILE_RUN_MS if run else TILE_WALK_MS)
        keys = f"{direction}+B" if run else direction
        ms = t * tiles - 3 * FRAME_MS
        self.press(keys, ms, gap=3 * FRAME_MS + 60)   # the last step finishes, then the next input
        self.facing = direction

    def pace(self, direction, trips=1):
        """Step back and forth on the spot: 2 * trips steps (VS Seeker needs 100 steps to recharge).
        Walked, not run: every step is a turn."""
        back = {"UP": "DOWN", "DOWN": "UP", "LEFT": "RIGHT", "RIGHT": "LEFT"}[direction]
        for _ in range(trips):
            self.walk(direction, 1)
            self.walk(back, 1)

    def face(self, direction):
        """Turn on the spot without stepping. A short tap in a new direction only turns the player;
        if we already face that way, do nothing (a tap there would take a step)."""
        if self.facing == direction:
            return
        self.press(direction, 2 * FRAME_MS, gap=150)
        self.facing = direction

    def close(self):
        try:
            self.release()
        finally:
            self.ser.close()


if __name__ == "__main__":
    p = Pad()
    print("연결됨:", p.port, p.version)
    p.press("A")
    p.close()
