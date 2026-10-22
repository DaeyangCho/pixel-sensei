#!/usr/bin/env python3
"""
LLM bench for the Pokemon FR JP study tool.
Loads the model once, runs each case twice (2nd round = warm numbers).

usage:
  python bench_llm.py                                   # 30B-A3B (default)
  python bench_llm.py mlx-community/Qwen3-8B-4bit       # compare with 8B
"""
import sys
import time

from mlx_lm import load, stream_generate

MODEL = sys.argv[1] if len(sys.argv) > 1 else "mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit"

SYS = """너는 포켓몬 파이어레드(일본판, 가나만 사용) 대사를 한국인 학습자에게 풀어주는 도우미야.
입력은 OCR 결과라 탁점(゛)·반탁점(゜)·촉음(っ) 오독이 있을 수 있어.
규칙:
- 교정은 [후보]에 주어진 것만 반영해. 후보가 없으면 원문을 그대로 둬. 이름을 추측해서 바꾸지 마.
- [단어] 힌트가 있으면 한자 표기와 뜻에 그대로 활용해.
- 아래 3줄만 출력해. 줄 머리표(JA/KANJI/KO)는 그대로 쓰고 다른 말은 쓰지 마.
JA: 교정된 가나 원문 (띄어쓰기 유지)
KANJI: 같은 문장을 일본어 한자·가나 혼용으로 (고유명사·외래어는 가타카나 유지, 한국어 금지)
KO: 자연스러운 한국어 번역 (포켓몬 이름은 한국 정발명)"""

SHOTS = [
    ("ゆけの！ リサード！\n[후보] リサード→リザード(리자드, 포켓몬)",
     "JA: ゆけっ！ リザード！\nKANJI: 行けっ！ リザード！\nKO: 가라! 리자드!"),
    ("きみの ポケモンを やすませて あげましょう",
     "JA: きみの ポケモンを やすませて あげましょう\nKANJI: 君の ポケモンを 休ませて あげましょう\nKO: 당신의 포켓몬을 쉬게 해 드릴게요"),
]

CASES = [
    ("A 힌트 없음", "あ！ やせいの ディウダが とびだしてきた！"),
    ("B 힌트 있음",
     "あ！ やせいの ディウダが とびだしてきた！\n"
     "[후보] ディウダ→ディグダ(디그다, 포켓몬)\n"
     "[단어] やせい=野生(야생) / とびだす=飛び出す(튀어나오다)"),
    ("C NPC 대사", "ポケモンを つかまえるなら モンスターボールを なげるんだ！"),
]

print(f"loading {MODEL} ...", flush=True)
t = time.time()
model, tok = load(MODEL)
print(f"loaded in {time.time() - t:.1f}s", flush=True)

for rnd in (1, 2):
    for name, user in CASES:
        msgs = [{"role": "system", "content": SYS}]
        for q, a in SHOTS:
            msgs += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
        msgs.append({"role": "user", "content": user})
        prompt = tok.apply_chat_template(msgs, add_generation_prompt=True, enable_thinking=False)
        t0, first, out, last = time.time(), None, "", None
        for r in stream_generate(model, tok, prompt, max_tokens=120):
            if first is None:
                first = time.time() - t0
            out += r.text
            last = r
        print(f"\n[{rnd}회차] {name} | 첫 토큰 {first * 1000:.0f}ms | prefill {last.prompt_tps:.0f} tok/s"
              f" | decode {last.generation_tps:.1f} tok/s | 총 {time.time() - t0:.1f}s"
              f" | mem {last.peak_memory:.1f}GB")
        print(out.strip(), flush=True)
