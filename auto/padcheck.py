"""Quick check: press a few buttons and show what the board reports (needs the debug firmware for status)."""
import sys
import time

from pad import Pad

home = "--home" in sys.argv
keys = [a for a in sys.argv[1:] if a != "--home"] or ["RIGHT", "RIGHT", "LEFT", "LEFT"]
p = Pad(stick_dirs=not home)   # --home: d-pad (HOME menu); default: stick (in the game)
print("연결:", p.port, p.version)
for k in keys:
    p.press(k, 100, gap=400)
    print("눌렀음:", k)
t = time.time()
while time.time() - t < 2.5:                 # board log: "# t=... mounted=1 ready=1"
    line = p.ser.readline().decode(errors="replace").strip()
    if line.startswith("#"):
        print("보드:", line)
p.close()
