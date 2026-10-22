"""Autopilot: fights battles and replays routes, watching the screen through the translator (run.sh).

Needs ./run.sh running (it serves the screen state at http://127.0.0.1:8770/state) and the board plugged in.

    python auto.py state                       # print what the autopilot sees (no buttons)
    python auto.py battle                      # fight the current / next battle to the end
    python auto.py battle --dry                # only say which move it would pick
    python auto.py route routes/onsen.txt --loop 10

Stop any time with Ctrl+C (all buttons are released).
Never presses B during battle text, so an evolution is never cancelled.
"""
import argparse
import json
import os
import re
import sys
import time
import unicodedata
import urllib.request

import battle_ai

URL = "http://127.0.0.1:8770/state"

# --- calibration (adjust tonight after watching it once) -----------------------------------------
# Double battle target select (Gen 3 code): the cursor starts on the foe whose HP box is on top;
# LEFT/UP moves to the other foe, RIGHT/DOWN to my own side (never press those).
TARGET_KEY = {0: None, 1: "LEFT"}      # foe index in HUD order (top first) -> key before A
MIN_TEXT_PX = 15                        # fewer "text" pixels than this in a box: no text in it
DARK_LUM = 25                           # screen brightness below this = black (door / cave fade)
PRESS_PAUSE = 0.5                       # route "press": wait after each one (dialog text needs time to print)
TARGET_SELECT_WAIT = 0.6                # still on the move list this long after A -> it is asking for a target
END_TEXTS = ["しょうぶに かった", "けいけんちを もらった", "にげきれた", "てにいれた", "めのまえが", "つかまえた"]
STOP_TEXTS = {                          # dialog text -> why we hand control back
    "わすれさせ": "기술 배우기 (어떤 기술을 잊을지 직접 골라줘)",
    "おぼえさせますか": "기술 배우기 질문",
    "つぎの ポケモンを つかいますか": "다음 포켓몬 선택",
    "ポケモンを えらんで": "포켓몬 선택 화면 (내 포켓몬이 쓰러짐)",
    "めのまえが まっしろ": "전멸",
    "めのまえが まっくら": "전멸",
    "いれかえますか": "교체 질문 (게임 설정 '전투 스타일: 계속'으로 바꾸면 안 나와)",
}


def norm(s):
    return unicodedata.normalize("NFKC", s or "").replace(" ", "").replace("　", "")


# macOS built-in sounds (/System/Library/Sounds): Basso Blow Bottle Frog Funk Glass Hero Morse Ping Pop Purr
# Sosumi Submarine Tink. "off" = silent. Listen to all: python auto.py sounds
SOUND_DONE = "Pop"        # finished normally
SOUND_STOP = "Submarine"  # stopped: needs you (move to learn, faint, lost position ...)


def play(name):
    if name and name != "off":
        os.system(f"afplay /System/Library/Sounds/{name}.aiff >/dev/null 2>&1 &")


def alert(msg, done=False):
    print(f"\n🔔 {msg}", flush=True)
    play(SOUND_DONE if done else SOUND_STOP)


class Stop(Exception):
    pass


class Screen:
    def __init__(self, url=URL):
        self.url = url

    def get(self):
        try:
            with urllib.request.urlopen(self.url, timeout=2) as r:
                return json.loads(r.read())
        except Exception as e:
            raise Stop(f"run.sh 화면 상태를 못 읽어요 ({e}) — run.sh 켜져 있는지 확인")

    @staticmethod
    def in_battle(s):
        return (s["box"] and s["battle_box"]) or (s["box"] and s["kind"] in ("menu", "moves"))

    @staticmethod
    def dialog(s):
        """A real overworld text box: box-shaped AND something written in it. A plain light floor at the
        bottom of the screen also looks like an empty white box (box=True, text_px≈0)."""
        return bool(s["box"]) and (bool(s["arrow"]) or s.get("text_px", 0) >= MIN_TEXT_PX)

    @staticmethod
    def text(s, max_age=3.0):
        """Last decoded text, if it belongs to what is on screen now."""
        if not s["box"] or s["now"] - s["lines_t"] > max_age:
            return ""
        return " ".join(s["lines"])


class Autopilot:
    def __init__(self, pad, screen, dry=False, verbose=True):
        self.pad, self.screen, self.dry, self.verbose = pad, screen, dry, verbose
        self.last_choice = None
        self.no_pp = set()                            # (pokemon, move) with no PP left, until a heal
        self._pp_noted = False

    def log(self, *a):
        if self.verbose:
            print(time.strftime("%H:%M:%S"), *a, flush=True)

    def press(self, keys, ms=90, gap=150):
        if self.dry:
            return
        self.pad.press(keys, ms, gap=gap)

    def wait_for(self, pred, timeout, poll=0.08):
        t0 = time.time()
        while time.time() - t0 < timeout:
            s = self.screen.get()
            if pred(s):
                return s
            time.sleep(poll)
        return None

    def check_stop_text(self, s):
        t = norm(Screen.text(s))
        if "げんきに" in t and self.no_pp:                # healed: PP is back
            self.log("회복 확인 → PP 없음 기억을 지워요")
            self.no_pp.clear()
        for k, why in STOP_TEXTS.items():
            if norm(k) in t:
                raise Stop(why + f"  「{Screen.text(s)}」")

    # ---------------------------------------------------------------- battle
    def line_done(self, s, st):
        """A text box line is finished printing: red arrow, or its text pixels unchanged for 0.7 s.
        st: dict kept by the caller. Also enforces the gap since our last A (capture lag): pressing A on a
        box that has already closed would talk to the person again, forever."""
        now = time.time()
        if s["text_px"] != st.get("px"):
            st["px"], st["since"] = s["text_px"], now
        if now - st.get("last_a", 0) < 0.6:
            return False
        return bool(s["arrow"]) or now - st["since"] > 0.7

    def press_line(self, st):
        self.press("A")
        st["last_a"], st["px"] = time.time(), None

    def wait_battle_start(self, timeout):
        """Wait for the battle screen; meanwhile advance the trainer's lines before it (A only when a line
        is complete: the red arrow, or no change for 1.5 s), at most one A every 0.6 s."""
        t0, st, presses = time.time(), {}, 0
        while time.time() - t0 < timeout:
            s = self.screen.get()
            if Screen.in_battle(s):
                return True
            if Screen.dialog(s) and self.line_done(s, st) and presses < 12:
                self.press_line(st)
                presses += 1
                t0 = max(t0, time.time() - timeout + 3)    # still talking: keep waiting a little
            time.sleep(0.08)
        return False

    def battle(self, wait_start=5.0, end_quiet=2.5):
        """Fight until the battle screen has been gone for end_quiet seconds."""
        if not self.wait_battle_start(wait_start):
            self.log("배틀이 시작되지 않았어요")
            return False
        self.log("배틀 시작")
        self._me, self._acted = None, ((), 0)       # nothing carried over from the previous battle
        gone_since, last_a = None, 0.0
        levelup_t, levelup_a = 0.0, 0
        ended, last_b, b_tries = False, 0.0, 0
        same_text, same_since = None, time.time()
        while True:
            s = self.screen.get()
            cur_text = tuple(s["lines"]) if s["box"] else None
            if cur_text != same_text:
                same_text, same_since = cur_text, time.time()
            # some lines wait for A without the red arrow (the trainer's last words): A after 2 s unchanged
            stuck = s["box"] and s["kind"] == "dialog" and time.time() - same_since > 2.0
            t_now = norm(Screen.text(s))
            if t_now and Screen.in_battle(s):
                ended = any(norm(e) in t_now for e in END_TEXTS) or (ended and "あがった" in t_now)
            if Screen.in_battle(s) and "あがった" in norm(Screen.text(s)):
                levelup_t, levelup_a = time.time(), 0  # "…レベル47に あがった!" -> the stat window comes next
            if not Screen.in_battle(s) and time.time() - levelup_t < 8 and levelup_a < 3:
                # level-up stat window covers the text box: A shows the new totals, A again closes it
                gone_since = None
                if time.time() - last_a > 0.8:
                    self.log("레벨업 능력치 창 → A")
                    self.press("A")
                    last_a, levelup_a = time.time(), levelup_a + 1
                time.sleep(0.08)
                continue
            if not Screen.in_battle(s):
                if Screen.dialog(s):                  # white box after the battle (money, evolution, NPC)
                    self.check_stop_text(s)
                    if (s["arrow"] or stuck) and time.time() - last_a > (0.4 if s["arrow"] else 2.0):
                        self.press("A")
                        last_a = time.time()
                    gone_since, b_tries = None, 0
                else:
                    gone_since = gone_since or time.time()
                    quiet = time.time() - gone_since
                    if (ended and quiet > end_quiet) or quiet > 8:
                        self.log("배틀 끝")
                        return True
                    if not ended and quiet > 1.2 and b_tries < 3 and time.time() - last_b > 1.5:
                        # no ending line yet and no text box: the Pokemon list / bag opened by mistake
                        self.log("대사창이 사라짐 (포켓몬 창/가방?) → B")
                        self.press("B", 80, 300)
                        last_b, b_tries = time.time(), b_tries + 1
                time.sleep(0.08)
                continue
            gone_since = None
            self.check_stop_text(s)
            if "ポイント" in t_now and self.last_choice and not self._pp_noted:
                # "わざの ポイントが ない!" -> this move is empty until the next heal
                key = (getattr(self, "_me", None), self.last_choice["move"])
                self.no_pp.add(key)
                self._pp_noted = True
                self.log(f"{key[0]}의 {key[1]}: PP 없음 → 회복할 때까지 안 써요")
            if s["kind"] == "menu":
                self.turn(s)
                time.sleep(0.3)                       # let the screen state catch up with our presses
            elif s["kind"] == "moves":                # we did not open it (or a target prompt): pick anyway
                acted = getattr(self, "_acted", ((), 0))
                if acted[0] == tuple(s["lines"]) and time.time() - acted[1] < 2.5:
                    time.sleep(0.08)                  # same move list we just used: stale frame, ignore
                    continue
                self.pick_move(s)
            elif (s["arrow"] and time.time() - last_a > 0.35) or (stuck and time.time() - last_a > 2.0):
                self.press("A")                       # next line of battle text (A only: B would stop an evolution)
                last_a = time.time()
            time.sleep(0.08)

    def acting(self, s):
        # while the menu is up, its text is current however long ago it was read
        t = " ".join(s["lines"]) if s.get("lines_kind") == "menu" else Screen.text(s, 5)
        m = re.search(r"(\S+?)は\s*どうする", t or "")
        if m:
            self._me = m.group(1)
            return self._me
        return getattr(self, "_me", None)

    def turn(self, s):
        # double battle: the menu text read so far may still be the previous Pokemon's ("ナッシーは どうする?")
        acted_t = getattr(self, "_acted", ((), 0))[1]
        s = self.wait_for(lambda x: x["lines_kind"] == "menu" and x["lines_t"] > acted_t, 3.0) or s
        me = self.acting(s)
        if self.dry:                                  # nothing is pressed: wait for you to open the move list
            if getattr(self, "_dry_menu", None) != (me, tuple(s["lines"])):
                self._dry_menu = (me, tuple(s["lines"]))
                self.log(f"차례: {me or '?'} — (--dry) 기술 화면을 직접 열면 고를 기술을 보여줄게")
            return
        self.log(f"차례: {me or '?'}")
        # the HP boxes are read right after the menu shows up: give it a moment on the first turn
        s = self.wait_for(lambda x: x.get("hud_t", 0) >= x.get("lines_t", 0) - 0.05, 1.5) or s
        time.sleep(0.35)                              # inputs during the menu's slide-in are dropped
        for attempt in range(2):
            self.press("UP", 60, 100)
            self.press("LEFT", 60, 100)               # たたかう is top-left, the cursor never wraps
            self.press("A")
            s2 = self.wait_for(lambda x: x["box"] and x["kind"] == "moves", 2.0)
            if s2:
                self.pick_move(s2, me)
                return
            cur = self.screen.get()
            if cur["kind"] == "menu" and cur["box"]:
                continue                              # A was dropped: try again
            # opened the Pokemon list or the bag by mistake: B is safe there, back to the menu
            self.log("싸우다 대신 다른 창이 열림 → B로 돌아가기")
            self.press("B", 80, 400)
            if not self.wait_for(lambda x: x["box"] and x["kind"] == "menu", 2.5):
                self.press("B", 80, 400)
                if not self.wait_for(lambda x: x["box"] and x["kind"] == "menu", 2.5):
                    raise Stop("배틀 메뉴로 못 돌아왔어요")
            time.sleep(0.35)
        raise Stop("기술 목록이 안 열려요")

    def field(self, s, me):
        """(me, my_lv, foes[(name, lv)], partner) from the HP boxes, cleaned up against the species list."""
        hud = s.get("hud") or []
        lv_re = re.compile(r"^L\s*[vVuU]?\s*\.?\s*(\d{1,3})$")
        mons, levels = [], []
        for h in hud:
            m = lv_re.match(h["name"].replace(" ", ""))
            if m:                                     # a level read on its own: attach after all names are known
                levels.append((h, int(m.group(1))))
                continue
            sp = battle_ai.clean_species(h["name"])
            if sp:
                lv = h.get("lv") if h.get("lv") and 2 <= h["lv"] <= 100 else None
                mons.append({"name": sp, "lv": lv, "side": h["side"], "y": h["y"]})
        for h, lv in levels:                          # give it to the box on the same row
            row = [x for x in mons if x["side"] == h["side"] and abs(x["y"] - h["y"]) < 0.05 and not x["lv"]]
            if row and 2 <= lv <= 100:
                row[-1]["lv"] = lv
        foes = [(x["name"], x["lv"]) for x in mons if x["side"] == "foe"]
        mine = [(x["name"], x["lv"]) for x in mons if x["side"] == "mine"]
        b = s.get("battle") or {}
        if not foes and b.get("foe"):
            foes = [(battle_ai.clean_species(b["foe"].get("ja", "")) or b["foe"].get("ja"), None)]
        if me:                                        # from "XXXは どうする?" (most reliable)
            me = battle_ai.clean_species(me) or me
        elif len(mine) == 1:                          # what is on screen now beats the battle log
            me = mine[0][0]
        elif not mine and b.get("mine"):              # log can be stale after a switch: last resort
            me = battle_ai.clean_species(b["mine"].get("ja", ""))
        my_lv, partner = None, None
        for n, lv in mine:
            if me and n == me:
                my_lv = lv
            else:
                partner = (n, lv)
        if not me and mine:
            (me, my_lv), partner = mine[0], (mine[1] if len(mine) > 1 else None)
        return me, my_lv, foes, partner

    def pick_move(self, s, me=None):
        me = me or getattr(self, "_me", None)
        lines = s["lines"] if s["lines_kind"] == "moves" else []
        if not lines:
            s = self.wait_for(lambda x: x["lines_kind"] == "moves" and x["now"] - x["lines_t"] < 3, 2.0) or s
            lines = s["lines"] if s["lines_kind"] == "moves" else []
        me, my_lv, foes, partner = self.field(s, me)
        if self.dry:                                  # watching only: say it once per screen
            key = (tuple(lines), me, tuple(foes))
            if key == getattr(self, "_dry_key", None):
                return
            self._dry_key = key
        open_t = s.get("lines_t", 0)                 # when this move list was read
        exclude, retries = set(), 0
        for i, mname in enumerate(lines):             # moves we already know are out of PP
            k, _ = battle_ai.find_move(mname)
            if (me, k) in self.no_pp:
                exclude.add(i)
        self._pp_noted = False
        while True:
            choice = battle_ai.choose(me or "", my_lv, lines, foes, partner, exclude=exclude) if lines and foes else None
            if choice is None:
                raise Stop(f"쓸 수 있는 기술이 없어요 (기술 {lines}, 상대 {foes}, 나 {me}, PP 없음 {sorted(exclude)})")
            self.log(f"{me} → {choice['move']} {choice['why']} (점수 {choice['score']})  [상대 인식: {foes}]")
            self._n_foes = len(foes)
            self.last_choice = choice
            if self.dry:
                return
            slot = choice["slot"]
            time.sleep(0.35)                          # inputs during the move list's slide-in are dropped
            nav_t = time.time()
            self.press("UP", 60, 120)
            self.press("LEFT", 60, 120)
            if slot in (1, 3):
                self.press("RIGHT", 60, 120)
            if slot in (2, 3):
                self.press("DOWN", 60, 120)
            time.sleep(0.1)
            # check the box on the right (cursor's move): PP left and type
            info = self.wait_for(lambda x: x.get("pp_t", 0) > nav_t, 0.9)
            if info is None:                          # cursor did not move (already there): the read on opening counts
                cur = self.screen.get()
                info = cur if cur.get("pp_t", 0) >= open_t - 0.2 else None
            if info is None:
                self.log("PP 칸을 못 읽었어요 — 확인 없이 진행")
                break
            pp, ty = info.get("pp"), battle_ai.type_from_ja(info.get("pp_type") or "")
            want = battle_ai.MOVES.get(choice["move"], {}).get("type")
            if ty and want and ty != want and retries < 2:
                retries += 1
                self.log(f"커서가 다른 기술에 있어요 (보이는 타입 {ty}, 고른 기술 {want}) → 다시 이동")
                continue
            if pp and pp[0] == 0:
                self.log(f"{choice['move']} PP 0 → 다른 기술")
                exclude.add(slot)
                self.no_pp.add((me, choice["move"]))
                continue
            if pp:
                self.log(f"PP {pp[0]}/{pp[1]}")
            break
        before = s["lines"]
        self.press("A")
        self._acted = (tuple(before), time.time())
        # double battle: if the move list is still up, it wants a target
        time.sleep(TARGET_SELECT_WAIT)
        s2 = self.screen.get()
        doubles = partner is not None or len(foes) > 1
        # singles never asks for a target: a list still up there is lag or the "no PP" message coming
        if doubles and s2["box"] and s2["kind"] == "moves" and s2["lines"] == before:
            tgt = choice.get("target")
            # with one foe left the game already points at it, and LEFT would move to MY partner
            key = TARGET_KEY.get(tgt) if tgt is not None and getattr(self, "_n_foes", 0) == 2 else None
            self.log(f"대상 선택: {choice.get('foe') or '남은 상대'}" + (f" ({key})" if key else ""))
            if key:
                self.press(key, 60, 120)
            self.press("A")
        # the capture lags ~0.2 s: wait until the move list is really gone before doing anything else,
        # or the next loop would pick a move again on the next Pokemon's menu
        self.wait_for(lambda x: not (x["box"] and x["kind"] == "moves" and x["lines"] == before), 3.0)
        self._acted = (tuple(before), time.time())

    # ---------------------------------------------------------------- overworld
    def until_free(self, timeout=30):
        """Advance any overworld dialog with A until no text box is left (A only on a finished line)."""
        t0, free_since, st = time.time(), None, {}
        while time.time() - t0 < timeout:
            s = self.screen.get()
            if Screen.in_battle(s):
                return "battle"
            if Screen.dialog(s):
                free_since = None
                self.check_stop_text(s)
                if self.line_done(s, st):
                    self.press_line(st)
            else:
                free_since = free_since or time.time()
                if time.time() - free_since > 1.0:
                    return "free"
            time.sleep(0.1)
        raise Stop(f"대화가 안 끝나요 (text_px={s.get('text_px')}, 화살표={s['arrow']}, 글: 「{Screen.text(s)}」)")

    def walk(self, direction, n, run=False, door=False, at=None):
        """Continuous move (fast). door=True: tile by tile, waiting out the fade when the map changes
        (a door or cave entrance eats ~1 s of input, so a timed hold would come up short)."""
        if door:
            return self.walk_door(direction, n, run, at)
        t0 = time.time()
        if not self.dry:
            self.pad.walk(direction, n, run)
        time.sleep(0.25)                              # capture lag
        s = self.screen.get()
        if s.get("dark_t", 0) > t0:
            alert(f"'{direction} {n}' 도중에 화면 전환(문/동굴)이 있었어요 → 이 줄 끝에 door 를 붙여줘")
        if Screen.in_battle(s):
            raise Stop(f"이동 중에 배틀이 시작됐어요 ({direction} {n}칸) — 위치가 어긋났을 수 있어요")
        if s["box"]:
            self.log(f"이동 후 대화창: 「{Screen.text(s)}」 (다음 줄에서 until_free로 넘겨줘)")

    def wait_bright(self, lum0=None, t0=None, timeout=6, look=0.7):
        """After a step: if the screen starts fading (map change), wait until the new map is shown.
        Watches for `look` seconds instead of one glance: the capture lags ~0.2 s and the fade starts
        a moment after the step, so a single early check missed it and blamed the NEXT tile.
        lum0 = brightness before the step, t0 = time before the step."""
        t0 = t0 or time.time()
        end, started = time.time() + look, False
        while time.time() < end:
            s = self.screen.get()
            lum = s.get("lum", 255)
            if lum < DARK_LUM or s.get("dark_t", 0) > t0 or (lum0 and lum < lum0 * 0.75):
                started = True
                break
            time.sleep(0.03)
        if not started:
            return False
        self.log("화면 전환 중… 기다리는 중")
        ts, seen_dark = time.time(), False
        while time.time() - ts < timeout:
            s = self.screen.get()
            lum = s.get("lum", 255)
            seen_dark |= lum < DARK_LUM or s.get("dark_t", 0) > t0
            if seen_dark and lum >= DARK_LUM and time.time() - s.get("dark_t", 0) > 0.5:
                time.sleep(0.3)                       # the player becomes movable a moment after the fade-in
                return True
            if not seen_dark and time.time() - ts > 1.5:
                return False                          # it was only a darker area, not a fade
            time.sleep(0.05)
        raise Stop("화면 전환이 안 끝나요")

    def _step_door(self, direction, run):
        """One tile, then: did the map change?"""
        lum0, t0 = self.screen.get().get("lum"), time.time()
        if not self.dry:
            self.pad.walk(direction, 1, run)
        return self.wait_bright(lum0, t0)

    def walk_door(self, direction, n, run=False, at=None):
        """A line that goes through a door / cave entrance (the map changes on the way).
        door=K: run K-1 tiles in one go, then tile by tile until the map changes (normally the very next
        tile), then the rest in one go. So a K that is 1 too big or any amount too small still works.
        door (no K): tile by tile from the start; the log says which K to write."""
        done, found = 0, None
        if at and at > 1 and not self.dry:
            lum0, t0 = self.screen.get().get("lum"), time.time()
            self.pad.walk(direction, at - 1, run)
            done = at - 1
            if self.wait_bright(lum0, t0, look=0.5):
                found = done                          # the change was at K-1
        while found is None and done < n:
            done += 1
            if self._step_door(direction, run):
                found = done
            s = self.screen.get()
            if Screen.in_battle(s):
                raise Stop(f"이동 중에 배틀이 시작됐어요 ({direction} {done}/{n}칸)")
        if found is None:
            alert(f"'{direction} {n}' 에서 화면 전환이 없었어요 — door 를 빼거나 칸 수를 확인해줘")
            return
        if found != at:
            self.log(f"전환: {found}칸째 → 이 줄을 'walk {direction} {n}{' run' if run else ''} door={found}' 로 바꾸면 "
                     "가장 빨라요")
        if n > found:
            self.walk(direction, n - found, run)

    def pace(self, direction, trips, run=False, length=1):
        """Back and forth (VS Seeker charge): trips x (length out + length back) steps.
        A wild battle here is fought, then pacing goes on. Longer lines are faster: the first tile after
        each turn is walked, the rest run (if run)."""
        back = {"UP": "DOWN", "DOWN": "UP", "LEFT": "RIGHT", "RIGHT": "LEFT"}[direction]
        for _ in range(trips):
            for d in (direction, back):
                if not self.dry:
                    self.pad.walk(d, length, run and length > 1)
                s = self.screen.get()
                if Screen.in_battle(s):
                    self.battle(wait_start=2)
                    self.until_free(10)


def talk(ap, direction, timeout=20):
    """Face `direction` and press A every 0.5 s until someone answers (a wandering trainer stepping in front).
    Rematch -> intro lines -> battle (fought). No rematch -> their usual line is closed. Nobody -> give up."""
    if not ap.dry:
        ap.pad.face(direction)
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = ap.screen.get()
        if Screen.in_battle(s):
            ap.battle(wait_start=2)
            ap.until_free(15)
            return "battle"
        if Screen.dialog(s):
            ap.log(f"말 걸림: 「{Screen.text(s)}」")
            if ap.until_free(20) == "battle" or ap.wait_for(Screen.in_battle, 3.0):   # transition ~2 s
                ap.battle(wait_start=2)
                ap.until_free(15)
                return "battle"
            return "talked"
        ap.press("A", 60, 150)
        time.sleep(0.35)
    ap.log(f"{timeout}초 동안 아무도 안 왔어요 ({direction})")
    return "nobody"


def load_route(path, depth=0):
    """Route lines without comments; 'include other.txt' pastes that file in (path relative to this one)."""
    out = []
    for l in open(path, encoding="utf-8"):
        l = l.split("#")[0].strip()
        if not l:
            continue
        if l.split()[0] == "include":
            if depth > 5:
                raise Stop("include 가 너무 깊어요")
            out += load_route(os.path.join(os.path.dirname(path), l.split()[1]), depth + 1)
        else:
            out.append(l)
    return out


def seeker(ap):
    """Use the registered VS Seeker (SELECT) and wait until its animation is over.
    Not charged yet -> stop (the spa round trip is 118 steps, so this means the route was cut short)."""
    t0 = time.time()
    ap.press("SELECT")
    s = ap.wait_for(Screen.dialog, 1.2)          # a text box only comes when it can't be used
    t = norm(Screen.text(s)) if s else ""
    if s:
        ap.log(f"배틀서처: 「{Screen.text(s)}」")
    if "じゅうでん" in t or "バッテリー" in t:
        ap.until_free(10)
        raise Stop("배틀서처가 아직 충전 안 됐어요 (100걸음 필요)")
    if s:
        ap.until_free(10)
    time.sleep(max(0.5, 3.3 - (time.time() - t0)))     # trainers' "!" and jump animations (~3.3 s in all)


def run_route(ap, path, loops=1, step=False):
    lines = load_route(path)
    total = 0
    for k in range(loops):
        ap.log(f"=== 루트 {k + 1}/{loops}")
        fought = 0
        for i, ln in enumerate(lines, 1):
            if step:                                   # check a new route line by line
                input(f"[{i}/{len(lines)}] {ln}  ← Enter로 실행 ")
            else:
                ap.log(f"[{i}/{len(lines)}] {ln}")
            p = ln.split()
            op, args = p[0], p[1:]
            if op == "walk":
                opts = args[2:]
                at = next((int(o[5:]) for o in opts if o.startswith("door=") and o[5:].isdigit()), None)
                ap.walk(args[0].upper(), int(args[1]) if len(args) > 1 else 1, "run" in opts,
                        "door" in opts or at is not None, at)
            elif op == "talk":                         # talk LEFT 20: wait for a wanderer in front, then talk
                if talk(ap, args[0].upper(), float(args[1]) if len(args) > 1 else 20) == "battle":
                    fought += 1
            elif op == "face":                         # face LEFT: turn without stepping
                if not ap.dry:
                    ap.pad.face(args[0].upper())
            elif op == "pace":                         # pace LEFT 50 [len=7] [run]: 50 x (7 out + 7 back) steps
                opts = args[2:]
                ln_ = next((int(o[4:]) for o in opts if o.startswith("len=") and o[4:].isdigit()), 1)
                ap.pace(args[0].upper(), int(args[1]) if len(args) > 1 else 50, "run" in opts, ln_)
            elif op == "press":                        # press A [ms] [pause s]: pause after it (default 0.5)
                ap.press(args[0].upper(), int(args[1]) if len(args) > 1 else 90)
                time.sleep(float(args[2]) if len(args) > 2 else PRESS_PAUSE)
            elif op == "wait":
                time.sleep(float(args[0]))
            elif op == "seeker":
                seeker(ap)
            elif op == "until_free":
                ap.until_free()
            elif op == "battle":
                ap.battle(wait_start=float(args[0]) if args else 6)
            elif op == "battles":                      # trainers one after another (VS Seeker rematches)
                while ap.battle(wait_start=float(args[0]) if args else 6):
                    ap.until_free(15)
            elif op == "expect":                       # expect <text> [seconds]: the screen must show this text
                want, sec = norm(args[0]), float(args[1]) if len(args) > 1 else 5
                if not ap.wait_for(lambda s: want in norm(Screen.text(s)), sec):
                    raise Stop(f"'{args[0]}' 가 안 보여요 — 위치가 어긋났을 수 있어요")
            else:
                raise Stop(f"모르는 명령: {ln}")
        total += fought
        if any(l.startswith("talk") for l in lines):
            ap.log(f"=== {k + 1}바퀴 끝: 재대결 {fought}번 (누적 {total}번)")


def main():
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("mode", choices=["state", "battle", "route", "sounds"])
    ap_.add_argument("route", nargs="?")
    ap_.add_argument("--loop", type=int, default=1)
    ap_.add_argument("--dry", action="store_true", help="watch and decide, but press nothing")
    ap_.add_argument("--step", action="store_true", help="route: wait for Enter before each line")
    ap_.add_argument("--port")
    ap_.add_argument("--sound", help="end sound (e.g. Hero, off)")
    ap_.add_argument("--stop-sound", help="sound when it stops and needs you")
    a = ap_.parse_args()
    global SOUND_DONE, SOUND_STOP
    SOUND_DONE, SOUND_STOP = a.sound or SOUND_DONE, a.stop_sound or SOUND_STOP
    if a.mode == "sounds":
        for n in "Basso Blow Bottle Frog Funk Glass Hero Morse Ping Pop Purr Sosumi Submarine Tink".split():
            print(n, flush=True)
            os.system(f"afplay /System/Library/Sounds/{n}.aiff")
            time.sleep(0.4)
        return
    screen = Screen()
    if a.mode == "state":
        while True:
            s = screen.get()
            print(json.dumps({k: s.get(k) for k in ("kind", "box", "battle_box", "arrow", "lines", "hud")},
                             ensure_ascii=False))
            time.sleep(1)
    pad = None
    if not a.dry:
        from pad import Pad
        pad = Pad(a.port)
    pilot = Autopilot(pad, screen, dry=a.dry)
    try:
        if a.mode == "battle":
            pilot.battle(wait_start=30)
        else:
            run_route(pilot, a.route, a.loop, a.step)
        alert("끝!", done=True)
    except Stop as e:
        alert(f"멈춤: {e}")
    except KeyboardInterrupt:
        print("\n중단")
    finally:
        if pad:
            pad.close()


if __name__ == "__main__":
    main()
