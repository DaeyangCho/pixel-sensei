"""Offline check of auto.py's battle loop with a fake screen that reacts to presses."""
import time
import auto

class Sim:
    def __init__(self, script):
        self.script, self.i, self.presses = script, 0, []
        self.t0 = time.time()
    def cur(self):
        st = dict(self.script[min(self.i, len(self.script) - 1)])
        now = time.time()
        base = {"box": True, "battle_box": True, "kind": "dialog", "arrow": False, "text_px": 500, "lines": [],
                "lines_kind": "dialog", "lines_t": now - 0.1, "hud": HUD, "hud_t": now, "now": now, "battle": {}}
        base.update(st)
        return base
    # screen
    def get(self):
        return self.cur()
    # pad
    def press(self, keys, ms=90, gap=150, wait=True):
        self.presses.append(keys)
        need = self.cur().get("advance_on")
        if need and keys == need:
            self.i += 1
    def walk(self, d, n=1):
        self.presses.append("walk " + d)
    def close(self): pass

HUD = [{"name": "オコリザル", "lv": 39, "side": "foe", "x": .03, "y": .08},
       {"name": "ゴーリキー", "lv": 39, "side": "foe", "x": .05, "y": .16},
       {"name": "レアコイル", "lv": 41, "side": "mine", "x": .55, "y": .27},
       {"name": "ナッシー", "lv": 42, "side": "mine", "x": .6, "y": .37}]
MOVES1 = ["スパーク", "10まんボルト", "ソニックブーム", "でんじは"]
MOVES2 = ["さいみんじゅつ", "タマゴばくだん", "サイコキネシス", "やどりぎのタネ"]
script = [
    {"lines": ["ゆけっ！ レアコイルと ナッシー！"], "arrow": True, "advance_on": "A"},
    {"kind": "menu", "lines": ["レアコイルは どうする？"], "lines_kind": "menu", "advance_on": "A"},
    {"kind": "moves", "lines": MOVES1, "lines_kind": "moves", "advance_on": "A"},
    {"kind": "moves", "lines": MOVES1, "lines_kind": "moves", "advance_on": "A"},   # target select
    {"kind": "menu", "lines": ["ナッシーは どうする？"], "lines_kind": "menu", "advance_on": "A"},
    {"kind": "moves", "lines": MOVES2, "lines_kind": "moves", "advance_on": "A"},
    {"kind": "moves", "lines": MOVES2, "lines_kind": "moves", "advance_on": "A"},
    {"lines": ["レアコイルの 10まんボルト！"], "arrow": True, "advance_on": "A"},
    {"lines": ["あいての ゴーリキーは たおれた！"], "arrow": True, "advance_on": "A"},
    {"lines": ["カラテおうの タクミ との", "しょうぶに かった！"], "arrow": True, "advance_on": "A"},
    {"box": False, "battle_box": False, "kind": None},
]
auto.TARGET_SELECT_WAIT = 0.01
sim = Sim(script)
ap = auto.Autopilot(sim, sim)
ok = ap.battle(wait_start=1, end_quiet=0.3)
print("battle ok:", ok)
print("presses:", sim.presses)

# stop rule: move learning question
sim2 = Sim([{"box": True, "battle_box": False, "lines": ["しかし わざを 4つ おぼえるので せいいっぱいだ！"], "arrow": True, "advance_on": "A"},
            {"box": True, "battle_box": False, "lines": ["ほかの わざを わすれさせますか？"]}])
sim2.script.insert(0, {"lines": ["カラテおうの タクミ との しょうぶに かった！"], "arrow": True, "advance_on": "A"})
try:
    auto.Autopilot(sim2, sim2).battle(wait_start=1, end_quiet=0.3)
except auto.Stop as e:
    print("stopped:", e)

# level-up stat window: box disappears for a while right after "レベル…に あがった"
sim3 = Sim([{"lines": ["ナッシーは", "レベル47に あがった！"], "arrow": True, "advance_on": "A"},
            {"box": False, "battle_box": False, "kind": None, "advance_on": "A"},     # stat window (+)
            {"box": False, "battle_box": False, "kind": None, "advance_on": "A"},     # stat window (totals)
            {"kind": "menu", "lines": ["ナッシーは どうする？"], "lines_kind": "menu"}])
ap3 = auto.Autopilot(sim3, sim3)
ap3.turn = lambda s: (_ for _ in ()).throw(auto.Stop("menu reached after level-up"))
try:
    ap3.battle(wait_start=1, end_quiet=0.3)
    print("level-up: battle wrongly ended")
except auto.Stop as e:
    print("level-up ok:", e)

# one foe left in a double battle: the target prompt must get a bare A (LEFT would hit my partner)
HUD1 = [h for h in HUD if h["name"] != "オコリザル"]
script4 = [
    {"kind": "menu", "lines": ["ナッシーは どうする？"], "lines_kind": "menu", "hud": HUD1, "advance_on": "A"},
    {"kind": "moves", "lines": MOVES2, "lines_kind": "moves", "hud": HUD1, "advance_on": "A"},
    {"kind": "moves", "lines": MOVES2, "lines_kind": "moves", "hud": HUD1, "advance_on": "A"},   # target prompt
    {"kind": "moves", "lines": MOVES2, "lines_kind": "moves", "hud": HUD1, "advance_on": "WAIT"},  # stale frame (capture lag)
    {"kind": "menu", "lines": ["ニドキングは どうする？"], "lines_kind": "menu", "hud": HUD1, "advance_on": "A"},
    {"lines": ["カラテおうの タクミ との", "しょうぶに かった！"], "arrow": True, "advance_on": "A"},
    {"box": False, "battle_box": False, "kind": None},
]
class LagSim(Sim):
    """the stale move-list frame stays for 0.5 s, then the next Pokemon's menu shows"""
    def get(self):
        st = self.cur()
        if self.script[min(self.i, len(self.script)-1)].get("advance_on") == "WAIT":
            if not hasattr(self, "_t"): self._t = time.time()
            if time.time() - self._t > 0.5: self.i += 1
        return self.cur()
sim4 = LagSim(script4)
_orig_turn = auto.Autopilot.turn
ap4 = auto.Autopilot(sim4, sim4)
seen = []
ap4.turn = lambda s: (seen.append(ap4.acting(s)), sim4.press("A"))
ap4.battle(wait_start=1, end_quiet=0.3)
print("one-foe presses:", sim4.presses, "next turn:", seen)

# trainer's last words: no arrow, waits for A
sim5 = Sim([{"lines": ["カラテおうの タクミ との", "しょうぶに かった！"], "arrow": True, "advance_on": "A"},
            {"lines": ["まいった！ つよいな きみ！"], "arrow": False, "advance_on": "A"},
            {"lines": ["おしえんは しょうきんとして 888円 てにいれた！"], "arrow": True, "advance_on": "A"},
            {"box": False, "battle_box": False, "kind": None}])
auto.Autopilot(sim5, sim5).battle(wait_start=1, end_quiet=0.3)
print("no-arrow presses:", sim5.presses)

# PP 0 on the best move: the box on the right says 0/10 -> take the next best
class PPSim(Sim):
    def get(self):
        st = self.cur()
        if st["kind"] == "moves":
            pressed = self.presses[-6:]
            on_psychic = "DOWN" in pressed and "RIGHT" not in pressed
            st.update(pp=(0, 10) if on_psychic else (15, 15), pp_type="エスパー" if on_psychic else "ノーマル",
                      pp_t=time.time())
        return st
sim6 = PPSim([
    {"kind": "moves", "lines": ["リフレクター", "のしかかり", "サイコキネシス", "ねむりごな"], "lines_kind": "moves",
     "hud": [{"name": "ゴーリキー", "lv": 39, "side": "foe", "x": .05, "y": .1}, {"name": "ナッシー", "lv": 46, "side": "mine", "x": .6, "y": .3}]},
])
ap6 = auto.Autopilot(sim6, sim6)
ap6._me = "ナッシー"
orig_press = sim6.press
def press6(keys, ms=90, gap=150, wait=True):
    orig_press(keys, ms, gap, wait)
    if keys == "A": raise auto.Stop("A pressed")
sim6.press = press6
try:
    ap6.pick_move(sim6.get(), "ナッシー")
except auto.Stop as e:
    print("pp test presses:", sim6.presses, "| last choice:", ap6.last_choice["move"])
