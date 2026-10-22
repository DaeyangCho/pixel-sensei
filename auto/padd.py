"""Pad server: the board stays player 1, and you play through it.

    python padd.py            # then open http://127.0.0.1:8772 in Chrome (phone: the LAN address it prints)

- Joy-Con / any controller paired to the Mac: read here (SDL, works while another window is focused) and
  passed straight to the board. Two Joy-Cons are joined into one controller.
- The web page: touch pad for the phone, keyboard for the Mac (hold = keep walking), job buttons, input check.
- Jobs run in this same process (the serial port has one owner): ZR = auto battle, ZL = VS Seeker charge,
  page buttons for the spa / rematch loop. Touching the stick or any button while a job runs stops it.
"""
import argparse
import collections
import json
import os
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")      # keep reading when OBS has the focus
os.environ.setdefault("SDL_JOYSTICK_HIDAPI_COMBINE_JOY_CONS", "1")      # L + R Joy-Con -> one controller
os.environ.setdefault("SDL_JOYSTICK_HIDAPI_VERTICAL_JOY_CONS", "1")     # a single Joy-Con held upright = half pad
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

NEUTRAL = (frozenset(), 128, 128)
# what each controller button does on the board (the GBA game reads the board's d-pad as buttons,
# so directions always go out as the left stick; − is the GBA SELECT = the board's d-pad left)
GAME_BUTTONS = {"A", "B", "X", "Y", "L", "R", "PLUS", "MINUS", "HOME", "CAPTURE"}
JOBS_BY_BUTTON = {"ZR": "battle", "ZL": "charge"}


# ---------------------------------------------------------------- input state from several sources
class Inputs:
    def __init__(self, sink):
        self.sources, self.lock, self.sink = {}, threading.Lock(), sink

    def set(self, src, buttons, lx=128, ly=128):
        with self.lock:
            self.sources[src] = (frozenset(buttons), int(lx), int(ly), time.time())
            self._apply()

    def drop(self, src):
        with self.lock:
            if self.sources.pop(src, None) is not None:
                self._apply()

    def combined(self):
        b, lx, ly = set(), 128, 128
        now = time.time()
        for s, (bs, x, y, t) in list(self.sources.items()):
            if s.startswith("web") and now - t > 3:          # a page that went away (phone locked): forget it
                continue
            b |= bs
            if (lx, ly) == (128, 128) and (abs(x - 128) > 20 or abs(y - 128) > 20):
                lx, ly = x, y
        return frozenset(b), lx, ly

    def _apply(self):
        self.sink(self.combined())


# ---------------------------------------------------------------- the server: board + jobs
class Server:
    def __init__(self, port=None, dry=False):
        self.dry = dry
        self.pad = None
        self.err = ""
        self.log = collections.deque(maxlen=60)
        self.job, self.job_thread = None, None
        self.abort = threading.Event()
        self.state = NEUTRAL
        self.sent = None
        self.controllers = []
        self.slock = threading.Lock()
        if not dry:
            try:
                from pad import Pad
                self.pad = Pad(port)
                t0 = time.perf_counter()
                for _ in range(20):
                    self.pad.cmd("?")
                rtt = (time.perf_counter() - t0) / 20 * 1000
                self.say(f"보드 연결됨 {self.pad.port} · 맥↔보드 왕복 {rtt:.1f} ms")
            except Exception as e:
                self.err = str(e)
                self.say(f"보드 연결 실패: {e}")
        self.inputs = Inputs(self.on_input)
        threading.Thread(target=self._keepalive, daemon=True).start()

    def say(self, msg):
        line = time.strftime("%H:%M:%S ") + msg
        self.log.append(line)
        print(line, flush=True)

    # ------------------------------------------------------------ manual input -> board
    def on_input(self, state):
        buttons, lx, ly = state
        prev, self.state = self.state, state
        for btn, job in JOBS_BY_BUTTON.items():             # ZR / ZL pressed: start that job
            if btn in buttons and btn not in prev[0]:
                if self.job:
                    self.stop_job("버튼으로 멈춤")
                else:
                    self.start_job(job)
                return
        moving = bool(buttons & GAME_BUTTONS) or abs(lx - 128) > 20 or abs(ly - 128) > 20
        if self.job:
            if moving:                                      # you took over: stop the job, then play
                self.stop_job("수동 조작으로 멈춤")
            else:
                return                                      # never release what the job is holding
        if self.job is None:
            self._send(buttons, lx, ly)
            if moving and self.pad is not None:
                self.pad.facing = None                       # we no longer know where the player faces

    def _send(self, buttons, lx, ly):
        from pad import BUTTONS, HAT_FORCED, NEUTRAL_HAT
        mask, hat = 0, NEUTRAL_HAT
        for b in buttons & GAME_BUTTONS:
            if b == "MINUS":
                hat = HAT_FORCED["HATLEFT"]                 # GBA SELECT (registered item, VS Seeker)
            else:
                mask |= BUTTONS[b]
        cmd = f"S {mask:x} {hat} {lx} {ly} 128 128"
        with self.slock:
            if cmd == self.sent:
                return
            self.sent = cmd
            if self.pad is not None:
                try:
                    self._write_fast(cmd)
                except Exception as e:
                    self.say(f"보드 오류: {e}")

    def _write_fast(self, cmd):
        """Write without waiting for the board's 'OK' (that wait was ~2-10 ms per change through the USB-serial
        bridge, and quick stick flicks queued up behind it). Old replies are simply thrown away."""
        with self.pad.lock:
            ser = self.pad.ser
            if ser.in_waiting:
                ser.reset_input_buffer()
            ser.write((cmd + "\n").encode())

    def _keepalive(self):
        """The board lets go after 10 s without a command: repeat a held input."""
        while True:
            time.sleep(2)
            if self.job is None and self.sent and self.sent != "S 0 8 128 128 128 128" and self.pad is not None:
                try:
                    self._write_fast(self.sent)
                except Exception:
                    pass

    # ------------------------------------------------------------ jobs
    def start_job(self, name, arg=None):
        if self.job:
            return False
        if self.pad is None and not self.dry:
            self.say("보드가 연결되지 않아서 실행할 수 없어요")
            return False
        self._send(frozenset(), 128, 128)                    # let go of everything first
        self.abort.clear()
        self.job = name
        self.job_thread = threading.Thread(target=self._run, args=(name, arg), daemon=True)
        self.job_thread.start()
        return True

    def stop_job(self, why="멈춤"):
        if self.job:
            self.abort.set()
            self.say(f"[{self.job}] {why}")

    def _run(self, name, arg):
        import auto
        label = {"battle": "자동 배틀", "charge": "충전", "onsen": "온천 루트", "loop": "재대결 루프"}.get(name, name)
        self.say(f"▶ {label} 시작")
        ap = auto.Autopilot(AbortPad(self.pad, self.abort), AbortScreen(auto.Screen(), self.abort), dry=self.pad is None)
        ap.log = self.say
        auto.alert = lambda m, done=False: (self.say("🔔 " + m), auto.play(auto.SOUND_DONE if done else auto.SOUND_STOP))
        try:
            if name == "battle":
                if ap.battle(wait_start=8):
                    ap.until_free(15)
            elif name in ("charge", "onsen", "loop"):
                route = HERE / "routes" / f"{name}.txt"
                auto.run_route(ap, str(route), loops=int(arg or 1))
            self.say(f"✓ {label} 끝")
            auto.play(auto.SOUND_DONE)
        except auto.Stop as e:
            self.say(f"■ {label} 멈춤: {e}")
            if not self.abort.is_set():
                auto.play(auto.SOUND_STOP)
        except Exception as e:
            self.say(f"■ {label} 오류: {e}")
        finally:
            try:
                if self.pad is not None:
                    self.pad.release()
            except Exception:
                pass
            self.sent = "S 0 8 128 128 128 128"
            self.job = None
            self.on_input(self.inputs.combined())             # whatever you are holding now takes effect

    def status(self):
        b, lx, ly = self.state
        return {"job": self.job, "log": list(self.log)[-14:], "controllers": self.controllers, "err": self.err,
                "board": getattr(self.pad, "port", None), "state": {"b": sorted(b), "lx": lx, "ly": ly}}


class AbortPad:
    """The board as the autopilot sees it, but every action first checks the stop flag."""
    WATCH = {"press", "walk", "hold", "face", "pace", "mash", "cmd"}

    def __init__(self, pad, flag):
        self._pad, self._flag = pad, flag

    def __getattr__(self, name):
        attr = getattr(self._pad, name)
        if name in self.WATCH and callable(attr):
            def wrapped(*a, **k):
                if self._flag.is_set():
                    import auto
                    raise auto.Stop("수동 조작")
                return attr(*a, **k)
            return wrapped
        return attr

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._pad, name, value)


class AbortScreen:
    def __init__(self, screen, flag):
        self._s, self._flag = screen, flag

    def get(self):
        if self._flag.is_set():
            import auto
            raise auto.Stop("수동 조작")
        return self._s.get()

    def __getattr__(self, name):
        return getattr(self._s, name)


# ---------------------------------------------------------------- macOS: no App Nap
def no_app_nap():
    """A background app's timers get coalesced by App Nap (tens of ms late). Tell macOS this process is
    latency-critical user activity (NSProcessInfo beginActivity), like a game would."""
    import ctypes
    import ctypes.util
    import sys
    if sys.platform != "darwin":
        return None
    try:
        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        ctypes.cdll.LoadLibrary(ctypes.util.find_library("Foundation"))
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.restype = ctypes.c_void_p
        send = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
        msg0 = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(send)
        msg_s = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p)(send)
        msg_act = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint64, ctypes.c_void_p)(send)
        sel = objc.sel_registerName
        info = msg0(objc.objc_getClass(b"NSProcessInfo"), sel(b"processInfo"))
        reason = msg_s(objc.objc_getClass(b"NSString"), sel(b"stringWithUTF8String:"), b"FireRed pad input")
        act = msg_act(info, sel(b"beginActivityWithOptions:reason:"), 0xFF00FFFFFF, reason)   # user-initiated + latency-critical
        msg0(act, sel(b"retain"))
        return act
    except Exception as e:
        print(f"App Nap 끄기 실패 (괜찮아요): {e}", flush=True)
        return None


# ---------------------------------------------------------------- controllers on the Mac (SDL)
def controller_loop(server, stop):
    """Main thread. Joy-Cons / Pro Controller / any pad paired to the Mac -> 'joy' input source."""
    try:
        import pygame
        from pygame._sdl2 import controller as sdlc
    except Exception as e:
        server.say(f"컨트롤러 읽기 끔 (pygame 없음: pip install pygame) — 휴대폰/키보드만 돼요 [{e}]")
        stop.wait()
        return
    pygame.display.init()
    pygame.joystick.init()
    sdlc.init()
    names = [("A", pygame.CONTROLLER_BUTTON_A), ("B", pygame.CONTROLLER_BUTTON_B),
             ("X", pygame.CONTROLLER_BUTTON_X), ("Y", pygame.CONTROLLER_BUTTON_Y),
             ("L", pygame.CONTROLLER_BUTTON_LEFTSHOULDER), ("R", pygame.CONTROLLER_BUTTON_RIGHTSHOULDER),
             ("PLUS", pygame.CONTROLLER_BUTTON_START), ("MINUS", pygame.CONTROLLER_BUTTON_BACK),
             ("HOME", pygame.CONTROLLER_BUTTON_GUIDE)]
    dpad = [(pygame.CONTROLLER_BUTTON_DPAD_UP, (128, 0)), (pygame.CONTROLLER_BUTTON_DPAD_DOWN, (128, 255)),
            (pygame.CONTROLLER_BUTTON_DPAD_LEFT, (0, 128)), (pygame.CONTROLLER_BUTTON_DPAD_RIGHT, (255, 128))]
    pads, last_scan, last = {}, 0.0, None
    while not stop.is_set():
        pygame.event.pump()
        if time.time() - last_scan > 2:                     # (re)connected controllers
            last_scan = time.time()
            for i in range(pygame.joystick.get_count()):
                if i not in pads and sdlc.is_controller(i):
                    try:
                        c = sdlc.Controller(i)
                        pads[i] = c
                        server.say(f"🎮 컨트롤러 연결: {c.name}")
                    except Exception:
                        pass
            for i, c in list(pads.items()):
                if not c.attached():
                    server.say(f"🎮 컨트롤러 끊김: {c.name}")
                    del pads[i]
            server.controllers = [c.name for c in pads.values()]
        buttons, lx, ly = set(), 128, 128
        for c in pads.values():
            for n, b in names:
                if c.get_button(b):
                    buttons.add(n)
            for b, st in dpad:
                if c.get_button(b):
                    lx, ly = st
            if c.get_axis(pygame.CONTROLLER_AXIS_TRIGGERRIGHT) > 16000:
                buttons.add("ZR")
            if c.get_axis(pygame.CONTROLLER_AXIS_TRIGGERLEFT) > 16000:
                buttons.add("ZL")
            ax, ay = c.get_axis(pygame.CONTROLLER_AXIS_LEFTX), c.get_axis(pygame.CONTROLLER_AXIS_LEFTY)
            if (lx, ly) == (128, 128) and (abs(ax) > 9000 or abs(ay) > 9000):
                lx = max(0, min(255, int(128 + ax / 256)))
                ly = max(0, min(255, int(128 + ay / 256)))
        cur = (frozenset(buttons), lx, ly)
        if cur != last:
            last = cur
            server.inputs.set("joy", *cur)
        time.sleep(0.002)


# ---------------------------------------------------------------- web
def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def start_web(server, port, token):
    page = HERE / "pad.html"

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _ok(self):
            if self.client_address[0] in ("127.0.0.1", "::1"):
                return True
            if f"k={token}" in self.path or self.headers.get("X-Pad-Key") == token:
                return True
            self._send(403, b"forbidden: open the address with ?k=... shown in the terminal", "text/plain")
            return False

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._ok():
                return
            path = self.path.split("?")[0]
            if path in ("/", "/pad"):
                return self._send(200, page.read_bytes(), "text/html; charset=utf-8")
            if path == "/status":
                return self._send(200, json.dumps(server.status(), ensure_ascii=False).encode(), "application/json")
            self._send(404, b"{}", "application/json")

        def do_POST(self):
            if not self._ok():
                return
            path = self.path.split("?")[0]
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if path == "/input":
                src = "web:" + str(req.get("id", self.client_address[0]))
                b = [x for x in req.get("b", []) if x in GAME_BUTTONS | set(JOBS_BY_BUTTON)]
                server.inputs.set(src, b, req.get("lx", 128), req.get("ly", 128))
                return self._send(200, b"{}", "application/json")
            if path == "/job":
                if req.get("job") == "stop":
                    server.stop_job("페이지에서 멈춤")
                else:
                    server.start_job(req.get("job"), req.get("n"))
                return self._send(200, json.dumps(server.status(), ensure_ascii=False).encode(), "application/json")
            self._send(404, b"{}", "application/json")

    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", help="board serial port (default: found automatically)")
    ap.add_argument("--web", type=int, default=8772)
    ap.add_argument("--dry", action="store_true", help="no board (test the page)")
    a = ap.parse_args()
    tok_file = HERE / "pad_token.txt"
    token = tok_file.read_text().strip() if tok_file.exists() else ""
    if not token:
        token = secrets.token_hex(3)
        tok_file.write_text(token)
    _activity = no_app_nap()  # noqa: F841  (kept alive for the whole run)
    server = Server(a.port, a.dry)
    start_web(server, a.web, token)
    server.say(f"맥: http://127.0.0.1:{a.web}   휴대폰(같은 와이파이): http://{lan_ip()}:{a.web}/?k={token}")
    stop = threading.Event()
    try:
        controller_loop(server, stop)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.stop_job("종료")
        if server.pad is not None:
            try:
                server.pad.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
