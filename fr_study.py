#!/usr/bin/env python3
"""
Pokemon FR (JP) study tool - capture -> template OCR -> name fix + hints -> local LLM -> panel

usage:
  python fr_study.py live --dev 0        # play; the panel opens at http://127.0.0.1:8770
  python fr_study.py demo                # panel + LLM with sample lines (no capture card)
  python fr_study.py try "あ！ やせいの/ディウダが とびだしてきた！"   # terminal only ('/' = line break)
  options: --model mlx-community/Qwen3-8B-4bit   --kanji   --port 8770   --no-panel

panel keys: O reading on/off · V voice on/off · R replay · W words · G grammar · S save · ↑/↓ pick a line
new glyphs: 🔤 button in the panel (http://127.0.0.1:8770/glyphs) - no need to stop the game

needs: fr_watch.py, glyph_ocr.py, jp_text.py (+ `python jp_text.py build` once), panel.html,
       mlx-lm, fugashi, unidic-lite.   30B model: `sudo sysctl iogpu.wired_limit_mb=19456` after reboot.
"""
import argparse
import json
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
import atexit
import os
import signal

# ffmpeg capture processes we started; killed on any exit so none outlive run.sh
_children = set()


def _kill_children(*_):
    for p in list(_children):
        if p.poll() is None:
            try:
                p.kill()
                p.wait(timeout=1)
            except Exception:
                pass
        _children.discard(p)


atexit.register(_kill_children)
for _sig in (signal.SIGTERM, signal.SIGHUP):          # terminal window closed / kill: still clean up
    signal.signal(_sig, lambda *a: (_kill_children(), os._exit(0)))


def reap_orphan_ffmpeg():
    """Kill capture ffmpeg left behind by an earlier run (its parent is gone, so ppid == 1)."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True).stdout
    except Exception:
        return 0
    n = 0
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[1] == "1" and "ffmpeg" in parts[2] \
                and "avfoundation" in parts[2] and "rawvideo" in parts[2]:
            try:
                os.kill(int(parts[0]), signal.SIGKILL)
                n += 1
            except Exception:
                pass
    return n

HERE = Path(__file__).parent
import livestate  # noqa: E402  (live screen state for auto.py: GET /state)
DEFAULT_MODEL = "mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit"
LOG = HERE / "logs"
LOG.mkdir(exist_ok=True)

# ---------------------------------------------------------------- prompts
SYS_HEAD = """너는 포켓몬 파이어레드(일본판, 가나만 사용) 대사를 한국인 학습자에게 풀어주는 도우미야.
입력 문장은 이미 교정된 원문이고, 아래에 힌트가 붙을 수 있어.
- [이름] [단어] 힌트는 그대로 활용해.
- [참고]는 자동 분석이라 틀릴 수 있어. 문맥에 맞을 때만 써.
- 원문을 고치거나 이름을 추측해서 바꾸지 마.
- 주인공(플레이어) 이름은 뜻으로 번역하지 말고 [이름]에 적힌 한국어 표기 그대로 써.
- [앞 문맥]은 같은 사람이 바로 앞에 한 말과 그 번역이야. 그건 번역하지 말고, 이어지는 뜻과 말투를 맞추는 데만 써.
- [미완성]이 붙으면 문장의 앞부분만 온 거야. 뒷부분을 지어내지 말고 온 부분만 자연스럽게 옮겨 (끝은 "…"로).
"""
KO_RULE = "KO: 자연스러운 한국어 번역 (포켓몬·기술·도구 이름은 한국 정발명)"
KANJI_RULE = "KANJI: 같은 문장을 일본어 한자·가나 혼용으로 (고유명사·외래어는 가타카나 유지, 한국어 금지)"
SHOT_DATA = [
    ("ゆけっ！ リザード！\n[이름] リザード(리자드, 포켓몬)", "가라! 리자드!", "行けっ！ リザード！"),
    ("きみの ポケモンを やすませて あげましょう\n[참고] (자동 분석이라 틀릴 수 있음) やすま→休む",
     "당신의 포켓몬을 쉬게 해 드릴게요", "君の ポケモンを 休ませて あげましょう"),
    ("よろしかったら これも おつかいに なって！\n[앞 문맥] それから・・・ → 그리고…",
     "괜찮으시면 이것도 써 주세요!", "よろしかったら これも お使いに なって！"),
]

WORDS_SYS = """너는 한국인을 위한 일본어 학습 도우미야. 포켓몬 파이어레드(가나만 사용) 대사에서
학습할 만한 핵심 단어 2~3개를 골라. 고유명사(포켓몬·사람 이름)는 빼. 힌트가 있으면 활용해.
한 줄에 하나씩, 정확히 이 형식으로만 써. 다른 말은 쓰지 마.
사전형(가나) | 한자 표기 | 한국어 뜻
예) とびだす | 飛び出す | 튀어나오다"""

GRAMMAR_SYS = """너는 한국인을 위한 일본어 학습 도우미야. 포켓몬 파이어레드(가나만 사용) 대사에서
알아두면 좋은 문법 포인트(조사, 동사 활용, 어미) 1~2개를 골라 한 줄씩 짧게 설명해.
정확히 이 형식으로만 써. 다른 말은 쓰지 마.
표현 → 한국어 설명
예) 〜てきた → 동작이 이쪽으로 다가옴 (~해 왔다)"""


def build_prompt(kanji):
    keys = ("KO", "KANJI") if kanji else ("KO",)
    sys_ = SYS_HEAD + (f"아래 {len(keys)}줄만 출력해. 줄 머리표({'/'.join(keys)})는 그대로 쓰고 다른 말은 쓰지 마.\n"
                       + KO_RULE + ("\n" + KANJI_RULE if kanji else ""))
    shots = [(q, f"KO: {ko}" + (f"\nKANJI: {kj}" if kanji else "")) for q, ko, kj in SHOT_DATA]
    return keys, sys_, shots


# ---------------------------------------------------------------- LLM
class Explainer:
    """Local LLM. The translation prompt (system + examples) lives in a reusable KV cache."""

    def __init__(self, model_name, kanji=False):
        self.keys, self.sys, self.shots = build_prompt(kanji)
        from mlx_lm import load
        from mlx_lm.models.cache import make_prompt_cache
        import mlx.core as mx
        t = time.time()
        self.model, self.tok = load(model_name)
        print(f"model loaded {time.time() - t:.1f}s  ({model_name})", flush=True)
        self._pin_weights(mx)
        a, b = self._render("あ"), self._render("ん")
        p = 0
        while p < min(len(a), len(b)) and a[p] == b[p]:
            p += 1
        self.prefix = a[:p]
        self.cache = make_prompt_cache(self.model)
        t = time.time()
        self.model(mx.array(self.prefix)[None], cache=self.cache)
        mx.eval([c.state for c in self.cache])
        print(f"prefix cached: {len(self.prefix)} tokens in {time.time() - t:.1f}s", flush=True)

    def _pin_weights(self, mx):
        """Keep the weights wired in GPU memory between lines. mlx-lm only wires them while generating, so
        when RAM is tight macOS pages them out between lines and the next line's prefill crawls (2 tok/s)."""
        try:
            from mlx.utils import tree_flatten
            weights = sum(v.nbytes for _, v in tree_flatten(self.model.parameters()))
            info = (getattr(mx, "device_info", None) or mx.metal.device_info)()
            limit = min(int(weights + 1.0e9), int(info["max_recommended_working_set_size"]))
            mx.set_wired_limit(limit)
            print(f"weights pinned: {weights / 1e9:.1f} GB (wired limit {limit / 1e9:.1f} GB)", flush=True)
        except Exception as e:
            print(f"weights not pinned ({e})", flush=True)

    def _tokens(self, msgs):
        out = self.tok.apply_chat_template(msgs, add_generation_prompt=True, enable_thinking=False)
        return self.tok.encode(out) if isinstance(out, str) else list(out)

    def _render(self, user):
        msgs = [{"role": "system", "content": self.sys}]
        for q, a in self.shots:
            msgs += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
        msgs.append({"role": "user", "content": user})
        return self._tokens(msgs)

    def explain(self, user, on_line=None, on_partial=None, max_tokens=150):
        """Streams; on_partial(key, text_so_far) while typing, on_line(key, value) when a line ends."""
        from mlx_lm import stream_generate
        from mlx_lm.models.cache import make_prompt_cache, trim_prompt_cache
        full = self._render(user)
        P = len(self.prefix)
        if full[:P] == self.prefix:
            cache, suffix = self.cache, full[P:]
        else:
            cache, suffix = make_prompt_cache(self.model), full
        res, buf, last = {}, "", None

        def emit(line):
            if ":" in line:
                k, v = line.split(":", 1)
                k, v = k.strip().upper(), v.strip()
                if k in self.keys and k not in res:
                    res[k] = v
                    if on_line:
                        on_line(k, v)

        try:
            for r in stream_generate(self.model, self.tok, suffix, max_tokens=max_tokens, prompt_cache=cache):
                buf += r.text
                last = r
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    emit(line)
                if len(res) == len(self.keys):
                    break
                if on_partial and ":" in buf:
                    k, v = buf.split(":", 1)
                    if k.strip().upper() in self.keys:
                        on_partial(k.strip().upper(), v.strip())
            emit(buf)
        finally:
            if cache is self.cache:
                extra = self.cache[0].offset - P
                if extra > 0:
                    trim_prompt_cache(self.cache, extra)
        if last is not None:
            res["_stats"] = f"prefill {last.prompt_tps:.0f} tok/s, decode {last.generation_tps:.1f} tok/s"
        return res

    def ask(self, system, user, on_text=None, max_tokens=200):
        """One-off question with its own system prompt (no prefix cache)."""
        from mlx_lm import stream_generate
        toks = self._tokens([{"role": "system", "content": system}, {"role": "user", "content": user}])
        out = ""
        for r in stream_generate(self.model, self.tok, toks, max_tokens=max_tokens):
            out += r.text
            if on_text:
                on_text(out)
        return out.strip()


def swap_used_mb():
    """macOS swap in use (MB); -1 if unknown. Growing swap while playing = memory is too tight."""
    try:
        out = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, timeout=2).stdout
        m = re.search(r"used = ([\d.]+)M", out)
        return round(float(m.group(1))) if m else -1
    except Exception:
        return -1


def gpu_peak_gb():
    try:
        import mlx.core as mx
        f = getattr(mx, "get_peak_memory", None) or mx.metal.get_peak_memory
        return round(f() / 1e9, 1)
    except Exception:
        return -1


# ---------------------------------------------------------------- panel server (SSE, stdlib only)
class Hub:
    def __init__(self):
        self.clients, self.lock = [], threading.Lock()

    def publish(self, event):
        data = json.dumps(event, ensure_ascii=False)
        with self.lock:
            for q in list(self.clients):
                q.put(data)

    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            self.clients.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)


def start_panel(study, cmd_q, port):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
            if self.path == "/":
                return self._send(200, (HERE / "panel.html").read_bytes(), "text/html; charset=utf-8")  # edits: just reload
            if self.path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            m = re.fullmatch(r"/sprite/(front|back)/(\d+)\.png", self.path)
            if m:                                         # FRLG sprites for the battle view (data/sprites)
                f = HERE / "data" / "sprites" / m.group(1) / f"{m.group(2)}.png"
                return self._send(200, f.read_bytes(), "image/png") if f.exists() else self._send(404, b"", "image/png")
            if (self.path == "/glyphs" or self.path.startswith("/glyphs/")) and study.glib:
                import glyph_ocr
                code, body, ctype = glyph_ocr.web_get(study.glib, self.path[len("/glyphs"):])
                return self._send(code, glyph_ocr.page_html("/glyphs") if body is None else body, ctype)
            if self.path == "/state":                     # every-frame screen state for auto.py
                body = json.dumps(livestate.snapshot(study.battle.state()), ensure_ascii=False).encode()
                return self._send(200, body, "application/json")
            if self.path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                q = study.hub.subscribe()
                self.wfile.write(f"data: {json.dumps(study.status())}\n\n".encode())
                if study.vocab:
                    self.wfile.write(f"data: {json.dumps(study.vocab.snapshot(), ensure_ascii=False)}\n\n".encode())
                try:
                    for c in study.cards[-40:]:
                        snap = json.dumps(dict(c, _snap=True), ensure_ascii=False)
                        self.wfile.write(f"data: {snap}\n\n".encode())
                    bs = json.dumps(study.battle.state(), ensure_ascii=False)
                    self.wfile.write(f"data: {bs}\n\n".encode())
                    self.wfile.flush()
                    while True:
                        try:
                            data = q.get(timeout=15)
                            self.wfile.write(f"data: {data}\n\n".encode())
                        except queue.Empty:
                            self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    study.hub.unsubscribe(q)
                return
            self._send(404, b"{}", "application/json")

        def do_POST(self):
            if self.path == "/glyphs/label" and study.glib:
                import glyph_ocr
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                res = glyph_ocr.web_label(study.glib, req)
                study.hub.publish(study.status())
                return self._send(200, json.dumps(res, ensure_ascii=False).encode(), "application/json")
            if self.path == "/vocab":                        # word rail: known / star
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                p = study.vocab_cmd(req)
                return self._send(200, json.dumps(p or {}, ensure_ascii=False).encode(), "application/json")
            if self.path == "/cmd":
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if req.get("cmd") == "ocr":                   # reading on/off, handled right here
                    study.set_reading(bool(req.get("on")))
                    return self._send(200, b'{"ok":true}', "application/json")
                cmd_q.put((req.get("cmd"), int(req.get("id", -1))))
                return self._send(200, b'{"ok":true}', "application/json")
            self._send(404, b"{}", "application/json")

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}"


class NoLLM:
    """--no-llm: no model in memory (autopilot runs). Templates and the cache of earlier lines still translate."""
    OFF = "(번역 꺼짐: --no-llm)"

    def explain(self, user, on_line=None, on_partial=None, max_tokens=150):
        return {"KO": self.OFF, "_nollm": True}

    def ask(self, system, user, on_text=None, max_tokens=200):
        return self.OFF


# ---------------------------------------------------------------- study logic
class Study:
    END = "。！？…」』"

    def __init__(self, model_name, kanji=False, llm=None):
        from jp_text import Hinter
        from templates import Templates
        from battle import Battle
        self.h = Hinter()
        self.tpl = Templates(self.h.gloss)
        self.battle = Battle(self.h.gloss)
        self.llm = llm or Explainer(model_name, kanji)
        self.hub = Hub()
        self.glib = None
        self.reading = threading.Event()
        self.reading.set()
        self.cards, self.memo, self.prev_lines, self.strips = [], {}, [], {}
        self.lock = threading.RLock()                # cards are changed by two threads (reader + translator)
        self.llm_lock = threading.Lock()             # one generation at a time
        self.utt = None                              # field speech being read: {"card", "u"}
        self.jobs = queue.PriorityQueue()            # sentences waiting: finals first, then drafts
        self._seq = 0
        threading.Thread(target=self._worker, daemon=True).start()
        try:                                         # word rail: game lexicon + what you have seen / know
            import vocab_live
            self.vocab = vocab_live.Vocab(self.h.gloss)
            atexit.register(self.vocab.save)
            print(f"word rail: {len(self.vocab.lex)} words, {sum(1 for p in self.vocab.prog.values() if p.get('status') == 'known')} known", flush=True)
        except Exception as e:
            print(f"[단어 레일] 끔: {e}", flush=True)
            self.vocab = None
        self._load_memo()
        if llm is None and not isinstance(self.llm, NoLLM):
            t = time.time()
            with self.llm_lock:
                self.llm.explain("はじめまして！")    # absorbs the slow first (compile) run
            print(f"llm warm-up {time.time() - t:.1f}s", flush=True)

    def _load_memo(self):
        """Translations from earlier sessions (logs/lines.jsonl) -> instant on repeat lines."""
        f = LOG / "lines.jsonl"
        if not f.exists():
            return
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            ja, ko = r.get("ja"), r.get("ko")
            if not ja or not ko:
                continue
            # skip old translations made before a custom name (player/rival) was known
            if any(n in ja and k not in ko for n, k in self.h.custom.items()):
                continue
            self.memo[ja] = {"KO": ko}
        if self.memo:
            print(f"translation cache: {len(self.memo)} lines from earlier sessions", flush=True)

    def status(self):
        return {"type": "status", "ocr": self.reading.is_set(),
                "glyphs": len(self.glib.pending) if self.glib else 0}

    def set_reading(self, on):
        (self.reading.set if on else self.reading.clear)()
        print(f"\n[읽기 {'켜짐' if on else '멈춤'}]", flush=True)
        self.hub.publish(self.status())

    def _words(self, c):
        """Word ids in this card (for the rail)."""
        c["vocab"] = self.vocab.find(c.get("ja", "")) if self.vocab else []

    def _count_words(self, c):
        """Once per card: seen +1 for its words; tell the panel the new counts."""
        if not self.vocab or not c.get("vocab") or c.get("_counted"):
            return
        c["_counted"] = True
        for w in self.vocab.seen_card(c["vocab"], c.get("ja"), c.get("ko") if c.get("ko_done") else None):
            self.hub.publish(self.vocab.entry(w))

    def vocab_cmd(self, req):
        if not self.vocab:
            return None
        p = self.vocab.set(req.get("id", ""), status=req.get("status"), star=req.get("star"))
        if p is not None:
            self.hub.publish(self.vocab.entry(req["id"]))
        return p

    def _push(self, card):
        with self.lock:
            self.hub.publish(card)

    def card(self, cid):
        return next((c for c in self.cards if c["id"] == cid), None)

    def handle(self, lines, t_trigger=None, unknown=0, strip=None, kind="dialog", extra=None):
        extra = extra or {}
        if kind == "box_end":                         # the field text box closed: the speech is over
            self.close_utt()
            return
        if kind == "dialog" and not extra.get("battle"):
            if isinstance(lines, str):
                lines = [l for l in lines.split("/") if l.strip()]
            return self.handle_utt([l.strip() for l in lines if l.strip()], t_trigger, unknown, strip, extra)
        self.close_utt()                              # a menu / battle text: the field speech is over
        if kind == "battle_end":
            if self.battle.end():
                self.hub.publish(self.battle.state())
            return
        if kind == "moves":
            return self.handle_moves(lines, strip)
        if kind == "place":
            return self.handle_place(lines[0] if lines else "")
        extra = extra or {}
        if isinstance(lines, str):
            lines = [l for l in lines.split("/") if l.strip()]
        lines = [l.strip() for l in lines if l.strip()]
        t0 = t_trigger or time.time()

        prev, ocr, merged = self.prev_lines, " ".join(lines), False
        if kind == "dialog" and prev and len(lines) > 1 and lines[0] == prev[-1]:
            prev_text = " ".join(prev)
            tail = prev_text[max(prev_text.rfind(c) for c in self.END) + 1:].strip()
            if tail:
                ocr, merged = tail + " " + " ".join(lines[1:]), True
        self.prev_lines = lines if kind == "dialog" else []
        item = None
        if extra.get("item"):
            name, _ = self.h.correct_and_hint(extra["item"])
            g = self.h.gloss.get(name)
            if g and g.get("kind") == "item":          # shop screens have no bag list: never show a garbled name
                item = {"ja": name, "hangul": self.h.to_hangul(name), "ko": g["ko"]}

        fixed, hints = self.h.correct_and_hint(ocr)
        if extra.get("after_resume") and self.cards and self.cards[-1]["ja"] == fixed:
            return                                    # same box that was on screen before the pause
        tko = self.tpl.translate(fixed)                # fixed battle messages: instant + exact
        in_battle = kind == "menu" or (kind == "dialog" and bool(extra.get("battle")))
        compact = bool(tko) and in_battle              # repeated battle lines: small rows in the battle view
        c = {"id": len(self.cards), "time": time.strftime("%H:%M:%S"), "ja": fixed, "ocr": ocr,
             "hangul": self.h.to_hangul(fixed), "ko": tko or "", "ko_done": bool(tko), "kanji": "", "merged": merged,
             "unknown": unknown, "words": None, "grammar": None, "saved": False, "kind": kind, "item": item,
             "compact": compact, "hints": "\n".join(l for l in hints.splitlines() if not l.startswith("[후보]"))}
        self._words(c)
        self.cards.append(c)
        self._count_words(c)
        if strip is not None:
            self.strips[c["id"]] = strip
            self.strips.pop(c["id"] - 60, None)
        if kind in ("dialog", "menu") and self.battle.feed(fixed, in_battle, ko=tko, compact=compact, pr=c["hangul"]):
            self.hub.publish(self.battle.state())
        self._push(c)

        def ts():
            return f"+{time.time() - t0:.1f}s"

        print(f"\n────────── {c['time']}" + ("   (이어진 문장)" if merged else "")
              + (f"   아이템: {item['ja']} ({item['ko'] or '?'})" if item else ""))
        print(f"원문  {fixed}\n발음  {c['hangul']}   ({ts()})", flush=True)

        if tko:
            r = {"KO": tko}
            with open(LOG / "lines.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "ocr": ocr, "ja": fixed,
                                    "hangul": c["hangul"], "ko": tko, "src": "template"}, ensure_ascii=False) + "\n")
        elif fixed in self.memo:
            r = self.memo[fixed]
        else:
            def partial(k, v):
                if k == "KO":
                    c["ko"] = v
                    self._push(c)

            t_llm = time.time()
            with self.llm_lock:
                r = self.llm.explain(fixed + ("\n" + c["hints"] if c["hints"] else ""), on_partial=partial)
            perf = {"wait": round(t_llm - t0, 1), "llm": round(time.time() - t_llm, 1), "stats": r.pop("_stats", "")}
            if perf["wait"] + perf["llm"] > 8:            # slow line: say where the time went
                perf["swap"] = swap_used_mb()
                perf["gpu_peak_gb"] = gpu_peak_gb()
                print(f"⚠ 느림  대기 {perf['wait']}s · 번역 {perf['llm']}s ({perf['stats']}) · "
                      f"스왑 사용 {perf['swap']} MB · GPU 메모리 최대 {perf['gpu_peak_gb']} GB", flush=True)
            if r.pop("_nollm", False):                  # never cache "translation off" as a translation
                c["ko"], c["ko_done"] = r["KO"], True
                self._push(c)
                print(f"뜻    {c['ko']}", flush=True)
                return
            self.memo[fixed] = r
            with open(LOG / "lines.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "ocr": ocr, "ja": fixed,
                                    "hangul": c["hangul"], "ko": r.get("KO"), "hints": hints, "perf": perf},
                                   ensure_ascii=False) + "\n")
        c["ko"], c["kanji"], c["ko_done"] = r.get("KO", ""), r.get("KANJI", ""), True
        self._push(c)
        print(f"뜻    {c['ko']}   ({ts()})" + (f"\n漢字  {c['kanji']}" if c["kanji"] else ""), flush=True)

    # ------------------------------------------------------------ field speech (several boxes -> one card)
    def handle_utt(self, lines, t0=None, unknown=0, strip=None, extra=None):
        """One field text box. Boxes of the same speech go into one card; sentences are translated as soon
        as they are complete, with the sentences before them as context."""
        import dialog_merge
        extra = extra or {}
        lines = dialog_merge.strip_arrow(lines, extra.get("arrow"))
        if not lines or dialog_merge.is_junk(lines):
            return
        with self.lock:
            last = getattr(self, "_closed_utt", None)
            if self.utt is None and last and time.time() - last[2] < 4 and last[1].paras \
                    and dialog_merge.similar(lines[0], last[1].paras[-1][-1]) and len(lines) > 1:
                # the box was taken for closed, but this one scrolls on from it: same speech, reopen it
                self.utt = {"card": last[0], "u": last[1]}
                last[0]["open"] = True
                self._log_box(last[0]["id"], [], "reopen", None)
            if self.utt is None:
                c = {"id": len(self.cards), "time": time.strftime("%H:%M:%S"), "kind": "utter", "ja": "",
                     "hangul": "", "ko": "", "ko_done": False, "kanji": "", "sents": [], "open": True, "boxes": 0,
                     "rev": 0, "say": "", "merged": False, "unknown": 0, "words": None, "grammar": None,
                     "saved": False, "hints": "", "item": None, "compact": False}
                self.cards.append(c)
                self.utt = {"card": c, "u": dialog_merge.Utterance()}
            c, u = self.utt["card"], self.utt["u"]
            how = u.add(lines)
            self._log_box(c["id"], lines, how, extra.get("arrow"))
            if how in ("same", "empty"):
                return
            c["boxes"], c["unknown"] = len(u.boxes), c["unknown"] + (unknown or 0)
            c["rev"] += 1
            c["say"] = self.h.correct_and_hint(" ".join(u.new))[0]      # what this box added (read aloud)
            if strip is not None:
                self.strips[c["id"]] = strip
                self.strips.pop(c["id"] - 60, None)
            # no red arrow = the last box of this speech (or a question): its last sentence is complete too
            self._refresh_utt(final=extra.get("arrow") is False)
        print(f"\n────────── {c['time']}   대화창 {c['boxes']}" + (f" ({how})" if c["boxes"] > 1 else ""), flush=True)
        print(f"원문  {c['say']}", flush=True)

    def _refresh_utt(self, final=False):
        """Re-split the speech into sentences; queue every newly completed sentence for translation."""
        c, u = self.utt["card"], self.utt["u"]
        old = c["sents"]
        sents = []
        for i, (raw, complete) in enumerate(u.sentences(final)):
            fixed, hints = self.h.correct_and_hint(raw)
            prev = old[i] if i < len(old) else None
            if prev is not None and prev["ja"] == fixed:
                s = prev
            else:
                hints = "\n".join(l for l in hints.splitlines() if not l.startswith("[후보]"))
                s = {"ja": fixed, "hangul": self.h.to_hangul(fixed), "ko": "", "done": False, "complete": False,
                     "queued": "", "hints": hints, "draft": False}
                if prev is not None and prev["ko"] and not prev["done"]:
                    s["ko"], s["draft"] = prev["ko"], True    # the sentence grew: keep the old draft on screen
            s["complete"] = complete
            sents.append(s)
        c["sents"] = sents
        for i, s in enumerate(sents):
            if s["complete"] and s["queued"] != "final":
                s["queued"] = "final"                     # final translation of the whole sentence
                self._seq += 1
                self.jobs.put((0, self._seq, c["id"], i, s["ja"], "final"))
            elif not s["complete"] and not s["queued"]:
                s["queued"] = "draft"                     # sentence still going: a draft of what is there so far
                self._seq += 1
                self.jobs.put((1, self._seq, c["id"], i, s["ja"], "draft"))
        self._sync_utt(c)

    def _sync_utt(self, c):
        c["ja"] = " ".join(s["ja"] for s in c["sents"])
        c["hangul"] = " ".join(s["hangul"] for s in c["sents"])
        c["ko"] = " ".join(s["ko"] for s in c["sents"] if s["ko"])
        c["ko_done"] = bool(c["sents"]) and all(s["done"] for s in c["sents"])
        c["hints"] = "\n".join(dict.fromkeys(l for s in c["sents"] for l in s["hints"].splitlines() if l))
        self._words(c)
        self._push(c)

    def close_utt(self):
        with self.lock:
            if self.utt is None:
                return
            c = self.utt["card"]
            self._refresh_utt(final=True)             # a sentence left open at the end is complete now
            c["open"] = False
            self._log_box(c["id"], [], "end", None)
            self._closed_utt = (c, self.utt["u"], time.time())
            self.utt = None
            self._push(c)
            self._count_words(c)

    def _log_box(self, cid, lines, how, arrow):
        """logs/boxes.jsonl: every field box as read, to check / tune the joining later."""
        try:
            with open(LOG / "boxes.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "utt": cid, "how": how,
                                    "arrow": arrow, "lines": lines}, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _worker(self):
        """Translates completed sentences one by one (in reading order), so reading never waits for the LLM."""
        while True:
            _, _, cid, i, ja, mode = self.jobs.get()
            try:
                self._translate_sent(cid, i, ja, mode)
            except Exception as e:
                print(f"\n[번역 오류] {e}", flush=True)
            finally:
                self.jobs.task_done()

    def _translate_sent(self, cid, i, ja, mode="final"):
        """mode 'final': the whole sentence (cached, logged). 'draft': the start of a sentence that continues in
        the next box; it is replaced by the final translation once the sentence is complete."""
        draft = mode == "draft"
        with self.lock:
            c = self.card(cid)
            if c is None or i >= len(c["sents"]) or c["sents"][i]["ja"] != ja:
                return                                    # the text changed meanwhile; a newer job has it
            s = c["sents"][i]
            if draft and (s["complete"] or s["done"]):
                return                                    # already complete: the final job does it
            ctx = [p for p in c["sents"][max(0, i - 2):i] if p["ko"]]
        t0 = time.time()
        tko = None if draft else self.tpl.translate(ja)
        src = "template" if tko else "memo" if ja in self.memo and not draft else "llm"
        if tko:
            ko = tko
        elif src == "memo":
            ko = self.memo[ja].get("KO", "")
        else:
            user = ja + ("\n" + s["hints"] if s["hints"] else "")
            if draft:
                user += "\n[미완성] 이 문장은 다음 대화창에서 이어져"
            if ctx:
                user += "\n[앞 문맥] " + " / ".join(f"{p['ja']} → {p['ko']}" for p in ctx)

            def partial(k, v):
                if k == "KO" and not draft:               # a draft shows when it is finished (no flicker)
                    with self.lock:
                        if c["sents"][i] is s:
                            s["ko"], s["draft"] = v, False
                            self._sync_utt(c)

            with self.llm_lock:
                r = self.llm.explain(user, on_partial=partial)
            stats = r.pop("_stats", "")
            ko = r.get("KO", "")
            if r.pop("_nollm", False):                    # never cache "translation off" as a translation
                src = "off"
                if draft:
                    return
            elif draft:
                pass                                      # drafts are never cached or logged
            elif ko:
                self.memo[ja] = {"KO": ko}
                with open(LOG / "lines.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "ocr": ja, "ja": ja,
                                        "hangul": s["hangul"], "ko": ko, "hints": s["hints"], "utt": cid,
                                        "ctx": len(ctx), "perf": {"llm": round(time.time() - t0, 1), "stats": stats}},
                                       ensure_ascii=False) + "\n")
        with self.lock:
            if draft:
                if s["complete"] or s["done"] or c["sents"][i] is not s:
                    return                                # the final translation got there first
                s["ko"], s["draft"] = ko, True
            else:
                s["ko"], s["done"], s["draft"] = ko, True, False
            self._sync_utt(c)
        print(("임시  " if draft else "뜻    ") + ko + ("   (앞 문맥 반영)" if ctx and src == "llm" else ""), flush=True)

    def drain(self):
        """Wait for every queued translation (try mode / tests)."""
        self.close_utt()
        self.jobs.join()

    def handle_place(self, raw):
        """Area-name popup (top-left when you enter a town / route / cave): name, reading, Korean, read aloud."""
        import place_names
        ja, ko = place_names.korean(raw)
        if not ja:
            return
        last = getattr(self, "_last_place", ("", 0.0))
        if last[0] == ja and time.time() - last[1] < 20:
            return                                    # the same popup again (slide in / out)
        self._last_place = (ja, time.time())
        c = {"id": len(self.cards), "time": time.strftime("%H:%M:%S"), "kind": "place", "ja": ja,
             "hangul": self.h.to_hangul(ja), "ko": ko, "ko_done": bool(ko), "kanji": "", "merged": False, "unknown": 0,
             "words": None, "grammar": None, "saved": False, "hints": "", "item": None, "compact": False}
        self._words(c)
        with self.lock:
            self.cards.append(c)
        self._push(c)
        print(f"\n────────── {c['time']}   장소: {ja}  {c['hangul']}  {ko or '?'}", flush=True)
        if not ko and not isinstance(self.llm, NoLLM):  # not in the list: let the model translate the name
            def tr():
                with self.llm_lock:
                    r = self.llm.explain(ja + "\n[참고] 포켓몬 파이어레드의 장소 이름이야")
                with self.lock:
                    c["ko"], c["ko_done"] = r.get("KO", ""), True
                self._push(c)
            threading.Thread(target=tr, daemon=True).start()

    def handle_moves(self, names, strip=None):
        """Move list: 4 names, Korean names straight from the move dictionary (no LLM)."""
        from battle import TYPES
        if not hasattr(self, "_moves3"):
            try:
                self._moves3 = json.loads((HERE / "auto" / "data" / "gen3.json").read_text(encoding="utf-8"))["moves"]
            except (OSError, ValueError, KeyError):
                self._moves3 = {}
        cat_ko = {"phys": "물리", "spec": "특수", "status": "변화"}
        items = []
        for ja in names:
            fixed, _ = self.h.correct_and_hint(ja)
            g = self.h.gloss.get(fixed)
            m = self._moves3.get(fixed, {})
            t = TYPES.get(m.get("type"), ("", ""))
            items.append({"ja": fixed, "hangul": self.h.to_hangul(fixed),
                          "ko": g["ko"] if g and g.get("kind") == "move" else "",
                          "type": m.get("type", ""), "tja": t[0], "tko": t[1], "power": m.get("power"),
                          "acc": m.get("acc"), "pp": m.get("pp"), "cat": cat_ko.get(m.get("cat"), "")})
        c = {"id": len(self.cards), "time": time.strftime("%H:%M:%S"), "kind": "moves", "moves": items,
             "ja": "、".join(i["ja"] for i in items), "hangul": "", "ko": " · ".join(i["ko"] or "?" for i in items),
             "ko_done": True, "kanji": "", "merged": False, "unknown": 0, "words": None, "grammar": None,
             "saved": False, "hints": ""}
        self.cards.append(c)
        if strip is not None:
            self.strips[c["id"]] = strip
        self._push(c)
        print(f"\n────────── {c['time']}   기술 목록")
        for i in items:
            print(f"  {i['ja']:<8} {i['hangul']:<10} {i['ko']}")

    def command(self, cmd, cid):
        c = self.card(cid) if cid >= 0 else (self.cards[-1] if self.cards else None)
        if c is None:
            return
        user = f"대사: {c['ja']}\n번역: {c['ko']}" + (f"\n{c['hints']}" if c["hints"] else "")
        if cmd in ("words", "grammar") and c.get("kind") == "moves":
            return
        if cmd in ("words", "grammar"):
            field, system = ("words", WORDS_SYS) if cmd == "words" else ("grammar", GRAMMAR_SYS)
            c[field] = []
            self._push(c)

            def stream(text):
                c[field] = [l.strip() for l in text.splitlines() if l.strip()]
                self._push(c)

            with self.llm_lock:
                out = self.llm.ask(system, user, on_text=stream)
            stream(out)
        elif cmd == "save":
            rec = {k: c.get(k) for k in ("time", "ja", "hangul", "ko", "words", "grammar", "moves", "item")}
            if c.get("sents"):
                rec["sents"] = [{k: s.get(k) for k in ("ja", "hangul", "ko")} for s in c["sents"]]
            rec["date"] = time.strftime("%Y-%m-%d")
            if c["id"] in self.strips:
                from PIL import Image
                d = LOG / "saved"
                d.mkdir(exist_ok=True)
                img = Image.fromarray(self.strips[c["id"]])
                img = img.resize((img.width * 4, img.height * 4), Image.NEAREST)
                rec["image"] = f"saved/{time.strftime('%Y%m%d_%H%M%S')}_{c['id']}.png"
                img.save(LOG / rec["image"])
            with open(LOG / "saved.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            c["saved"] = True
            self._push(c)
            print(f"      ★ saved: {c['ja']}", flush=True)


# ---------------------------------------------------------------- capture
def capture_thread(dev, out_q, stop, reading=None, glib=None, on_glyphs=None):
    """reading: threading.Event; while it is cleared the camera is released (no CPU, no OCR).
    glib: shared GlyphLib (the panel's label page edits the same one). on_glyphs(n_pending) on changes."""
    import numpy as np
    from fr_watch import (BOX_Y0, BOX_Y1, FPS, GBA_X0, GBA_Y0, INNER, MIN_TEXT_PX, NATIVE_H, NATIVE_W, SCALE,
                          STABLE_NO_ARROW, STABLE_WITH_ARROW, analyze, ocr_image_orig, run_ocr, to_native)
    from glyph_ocr import MOVES_ORIGINS, GlyphLib, bag_item_inner, detect_layout
    glib = glib or GlyphLib()
    last_seen = {}                                # menu / move list / bag: only emit when the text changes
    print(f"glyph library: {len(glib.lib)} glyphs, {len(glib.pending)} pending", flush=True)
    # whole GBA frame (the bag screen needs the item list above the text box)
    w, h = int(NATIVE_W * SCALE), int(NATIVE_H * SCALE)
    sy0, sy1 = int(BOX_Y0 * SCALE), int(BOX_Y0 * SCALE) + int((BOX_Y1 - BOX_Y0) * SCALE)
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "avfoundation", "-framerate", "60",
           "-video_size", "1920x1080", "-pixel_format", "uyvy422", "-i", f"{dev}:none",
           "-vf", f"crop={w}:{h}:{GBA_X0}:{GBA_Y0},fps={FPS}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    n = w * h * 3

    def on():
        return reading is None or reading.is_set()

    def stop_ffmpeg(p):
        # close our end first: ffmpeg handles SIGTERM by flushing its output, and with a full,
        # unread pipe that flush blocks forever (this froze resume after a pause)
        try:
            p.stdout.close()
        except Exception:
            pass
        p.terminate()
        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        _children.discard(p)

    def frames():
        """Yields frames while reading is on; restarts ffmpeg after a pause."""
        fails = 0
        while not stop.is_set():
            if not on():
                time.sleep(0.1)
                continue
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
            _children.add(p)
            got = 0
            try:
                while not stop.is_set() and on():
                    buf = p.stdout.read(n)
                    if len(buf) < n:
                        break                      # ffmpeg ended (camera busy / unplugged)
                    yield got == 0, buf
                    got += 1
            finally:
                stop_ffmpeg(p)
            if on() and not stop.is_set():         # ended by itself, not by a pause
                fails = fails + 1 if got < 30 else 1
                if fails > 5:
                    print("\n캡처 장치를 열 수 없어요 (케이블/장치 번호 확인)", flush=True)
                    return
                print(f"\n캡처가 끊겼어요, 다시 연결하는 중… ({fails}/5)", flush=True)
                time.sleep(1.0)

    frames_dir = HERE / "debug" / "frames"           # last 300 read screens, to tune new screen layouts
    frames_dir.mkdir(parents=True, exist_ok=True)

    def save_frame(native, kind):
        try:
            from PIL import Image
            Image.fromarray(np.ascontiguousarray(native)).save(frames_dir / f"{time.strftime('%m%d_%H%M%S')}_{kind}.png")
            old = sorted(frames_dir.glob("*.png"))
            for f in old[:-300]:
                f.unlink()
        except Exception:
            pass

    try:
        from snaps import Snapper
        snapper = Snapper(SCALE, run_ocr)
    except Exception as e:
        print(f"[사진] 끔: {e}", flush=True)
        snapper = None
    last_hash, stable, emitted = None, 0, None
    started, after_resume, n_pending = False, False, len(glib.pending)
    battle_on, last_battle_t = False, 0.0
    field_open, last_text_t = False, 0.0            # a field text box with text in it is up
    try:
        for fresh, buf in frames():
            if fresh:
                last_hash, stable, emitted = None, 0, None
                after_resume, started = started, True     # first box after a pause may be the old one
            raw_full = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            raw = raw_full[sy0:sy1]
            strip = to_native(raw, BOX_Y1 - BOX_Y0)
            kind, inner = detect_layout(strip)
            inner = inner or INNER
            info = analyze(strip, inner)
            native = to_native(raw_full, NATIVE_H)
            now = time.time()
            livestate.on_frame(kind, info, now, native)
            # field speech ends when no text has been in a white box for 0.8 s (page turns take < 0.2 s),
            # or a menu / battle box takes over
            # (a single misread frame - menu / battle colours during a scroll - must not end it: 0.5 s at least)
            if info["box"] and not info["battle_box"] and kind == "dialog" and info["text_px"] >= MIN_TEXT_PX:
                field_open, last_text_t = True, now
            elif field_open and (now - last_text_t > 0.8 or
                                 ((info["battle_box"] or kind != "dialog") and now - last_text_t > 0.5)):
                field_open = False
                out_q.put(([], now, 0, None, "box_end", {}))
            if (info["box"] and info["battle_box"]) or kind in ("menu", "moves"):
                last_battle_t = now                       # battle screen still up
            elif battle_on and now - last_battle_t > 8:  # left the battle screen (not just a quick bag peek)
                battle_on = False
                out_q.put(([], now, 0, None, "battle_end", {}))
            if snapper is not None:
                try:
                    place = snapper.on_frame(native, raw_full)    # area-name popup -> panel card (+ town photo)
                    if place:
                        out_q.put(([place], now, 0, None, "place", {}))
                except Exception as e:                   # photos are a bonus: never stop reading for them
                    print(f"\n[사진] 오류: {e}", flush=True)
                    snapper = None
            for k in [k for k in last_seen if k != kind or not info["box"]]:
                del last_seen[k]                          # that screen closed: show it again next time it opens
            if not info["box"] or info["text_px"] < MIN_TEXT_PX:
                last_hash, stable, emitted = None, 0, None
                continue
            if info["hash"] == last_hash:
                stable += 1
            else:
                last_hash, stable = info["hash"], 0
            need = STABLE_WITH_ARROW if info["arrow"] else STABLE_NO_ARROW
            if stable != need or info["hash"] == emitted:
                continue
            emitted = info["hash"]
            t = time.time()
            vision = lambda: run_ocr(ocr_image_orig(raw, info, inner))  # noqa: E731
            if kind == "moves":
                lines, unk = glib.decode_grid(
                    strip, info, inner, MOVES_ORIGINS, lexicon=getattr(glib, "move_lexicon", None),
                    vision_at=lambda r: run_ocr(ocr_image_orig(raw, info, r)))
            else:
                text, unk = glib.decode(strip, info, inner, vision=vision)
                lines = [l for l in text.split("\n") if l.strip()]
            extra = {}
            if kind == "bag":                         # selected item name from the list above
                ni = bag_item_inner(native)
                if ni:
                    infon = analyze(native, ni)
                    name, _ = glib.decode(native, infon, ni,
                                          vision=lambda: run_ocr(ocr_image_orig(raw_full, infon, ni)))
                    extra["item"] = name.replace("\n", "").strip()
            if glib.dirty:
                glib.save()
            if on_glyphs and len(glib.pending) != n_pending:
                n_pending = len(glib.pending)
                on_glyphs(n_pending)
            if lines:
                livestate.on_lines(kind, lines, t, raw_full, SCALE, BOX_Y0)
            if kind != "dialog":
                sig = (lines, extra.get("item"))
                if last_seen.get(kind) == sig:
                    continue
                last_seen[kind] = sig
            if lines and kind == "dialog" and snapper is not None:
                try:
                    snapper.on_dialog(" ".join(lines), native)   # gym leader's battle intro -> photo
                except Exception:
                    pass
            if lines:
                if after_resume:
                    extra["after_resume"], after_resume = True, False
                extra["battle"] = bool(info["battle_box"])  # dark-teal box = battle text
                extra["arrow"] = bool(info["arrow"])        # no red arrow = last box of a speech
                if extra["battle"] or kind in ("menu", "moves"):
                    battle_on = True
                save_frame(native, kind)
                out_q.put((lines, t, unk, np.ascontiguousarray(strip), kind, extra))
    finally:
        out_q.put(None)
        if glib.pending:
            print(f"\n새 글자 {len(glib.pending)}개 확인 대기 → python glyph_ocr.py label", flush=True)


DEMO = [[["あ！ やせいの", "ディウダが とびだしてきた！"]], [["ゆけっ！ リザード！"]],
        [["それでは おあずかりいたします！"]],
        [["おまちどおさまでした！", "おあずかりした ポケモンは、"],         # one speech, 2 boxes (scroll)
         ["おあずかりした ポケモンは、", "みんな げんきに なりましたよ！"]]]


def run_loop(study, line_q, cmd_q):
    while True:
        try:
            study.command(*cmd_q.get_nowait())
            continue
        except queue.Empty:
            pass
        try:
            item = line_q.get(timeout=0.05)
        except queue.Empty:
            continue
        if item is None:
            break
        items, ended = [item], False
        while not line_q.empty():                # fell behind: keep every field box, skip stale other screens
            nxt = line_q.get_nowait()
            if nxt is None:
                ended = True
                break
            items.append(nxt)
        for k, it in enumerate(items):
            kind = it[4] if len(it) > 4 else "dialog"
            ex = (it[5] if len(it) > 5 else None) or {}
            if kind in ("box_end", "battle_end") or (kind == "dialog" and not ex.get("battle")) or k == len(items) - 1:
                study.handle(*it)
        if ended:
            return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["live", "demo", "try"])
    ap.add_argument("text", nargs="*")
    ap.add_argument("--dev", default="0")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--kanji", action="store_true")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--no-panel", action="store_true")
    ap.add_argument("--paused", action="store_true", help="start with reading paused")
    ap.add_argument("--no-llm", action="store_true", help="no translation model (autopilot: saves ~17 GB)")
    a = ap.parse_args()

    study = Study(a.model, a.kanji, llm=NoLLM() if a.no_llm else None)
    if a.mode == "live":
        from glyph_ocr import GlyphLib, move_lexicon
        study.glib = GlyphLib()
        study.glib.move_lexicon = move_lexicon(study.h.gloss)
        study.glib.corrector = study.h.lexicon_spans
    if a.mode == "try":
        for t in a.text or ["あ！ やせいの/ディウダが とびだしてきた！"]:
            study.handle(t, extra={"arrow": True})       # each argument = one box of the same speech
        study.drain()
        return

    line_q, cmd_q, stop = queue.Queue(), queue.Queue(), threading.Event()
    if not a.no_panel:
        import webbrowser
        url = start_panel(study, cmd_q, a.port)
        print(f"panel: {url}", flush=True)
        webbrowser.open(url)
    if a.mode == "demo":
        def feed():
            time.sleep(3)
            for speech in DEMO:                       # each speech: its boxes, then the box closes
                for k, lines in enumerate(speech):
                    line_q.put((lines, None, 0, None, "dialog", {"arrow": k < len(speech) - 1}))
                    time.sleep(4)
                line_q.put(([], None, 0, None, "box_end", {}))
                time.sleep(2)
        threading.Thread(target=feed, daemon=True).start()
    else:
        from fr_watch import warmup
        n = reap_orphan_ffmpeg()
        if n:
            print(f"이전 실행에서 남은 ffmpeg {n}개 정리", flush=True)
        warmup()
        if a.paused:
            study.reading.clear()
        threading.Thread(target=capture_thread, daemon=True,
                         args=(a.dev, line_q, stop, study.reading, study.glib,
                               lambda n: study.hub.publish(study.status()))).start()
    print("\nready (Ctrl+C to stop)", flush=True)
    try:
        run_loop(study, line_q, cmd_q)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        _kill_children()


if __name__ == "__main__":
    main()
