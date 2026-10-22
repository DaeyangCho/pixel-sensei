# pixel-sensei — 일본어판 포켓몬 파이어레드로 일본어 공부하기

> Play Japanese Pokémon FireRed on a Nintendo Switch and get a live Korean study panel next to the game:
> capture card → pixel-exact glyph OCR → local LLM translation (MLX, Apple Silicon) → browser panel with
> pronunciation, kanji, word cards and TTS. An ESP32-S3 board acting as a wired controller adds
> Joy-Con/phone control through the Mac and battle autopilot. Korean docs below.

스위치 화면을 캡처카드로 맥에 받아서, 대사가 뜨는 순간 **일본어 원문 · 한글 발음 · 한자 표기 · 한국어 번역 ·
단어 풀이**를 옆 브라우저 창에 띄워 준다. 번역은 맥 안의 로컬 LLM이 하고, 인터넷은 쓰지 않는다.
여기에 ESP32-S3 보드를 스위치 유선 컨트롤러로 꽂아 두면 맥에서 조이콘·휴대폰으로 게임을 조종하고,
반복 배틀은 자동으로 돌릴 수 있다.

파이어레드는 한자 없이 가나만 쓰고 띄어쓰기가 있어서, 가나를 막 뗀 사람이 문장을 끝까지 읽어 보기에 좋다.

## 무엇을 해 주나

| 기능 | 설명 |
|---|---|
| 실시간 대사 번역 | A를 여러 번 눌러야 하는 긴 문장도 상자들을 **한 문장으로 합쳐서** 번역. 문장이 끝나면 앞부분 번역을 전체 문맥으로 다시 고친다 |
| 발음 · 한자 | `あいての リザードン` → `아이테노 리자아돈` / `相手の リザードン`. 조사 は·へ는 와·에로 읽는다 |
| 단어 레일 | 대사 속 단어를 게임 어휘 사전(`lexicon_src.txt`, 370여 개, 활용형 포함)에서 찾아 아직 안 외운 것만 옆에 띄운다. 외웠다고 누르면 다음부터 안 나온다 |
| 배틀 화면 | 양쪽 포켓몬 그림 · 타입, 기술 4개의 타입/위력/명중/PP. 배틀 대사는 일본어로 읽어 준다 |
| 지명 팝업 | 마을·도로에 들어갈 때 왼쪽 위에 뜨는 이름을 한국 정발명으로 번역하고 읽어 준다 |
| 음성 | 브라우저 일본어 TTS로 대사 읽기 (V 키) |
| 자동 조종 (`auto/`) | 조이콘·휴대폰 → 맥 → 보드 → 스위치. 자동 배틀, 걷기 루트, 배틀서처 재대결 루프 |

## 전체 구조

```
 ┌──────────┐ HDMI ┌─────────┐ USB ┌──────────────────────────── Mac (Apple Silicon) ─────────────────────────────┐
 │  Switch  │────▶│ 캡처카드 │────▶│ ffmpeg 1080p60                                                                 │
 │ (일본어판 │      └─────────┘     │   └▶ fr_watch.py   GBA 화면 240×160 복원 · 대사 상자 찾기 · "다 찍혔나" 판단       │
 │ 파이어레드)│                     │       └▶ glyph_ocr.py  글자 비트맵 매칭 (모르는 글자만 Apple Vision OCR → 자동 학습)  │
 │          │                     │           └▶ jp_text.py · templates.py  이름 교정, 단어 힌트, 한글 발음           │
 │          │                     │               └▶ dialog_merge.py  여러 상자를 한 문장으로                        │
 │          │                     │                   └▶ 로컬 LLM (mlx-lm, Qwen3-30B-A3B 4bit)  번역 · 한자 표기     │
 │          │                     │                       └▶ panel.html  http://127.0.0.1:8770  (SSE로 실시간)       │
 │          │                     │   livestate.py  HP/PP/메뉴 상태 → GET /state ──┐                               │
 │          │ USB HID ┌─────────┐ │                                                 ▼                               │
 │   1P ◀───┼────────│ ESP32-S3│◀┼── USB 시리얼 ── auto/padd.py ◀── 조이콘(블루투스) · 휴대폰(와이파이) · auto.py   │
 └──────────┘         │ (frpad) │ └───────────────────────────────────────────────────────────────────────────────┘
                      └─────────┘
```

화면 읽기 쪽(위)과 조종 쪽(아래)은 따로 쓸 수 있다. **번역 패널만 쓰려면 보드는 필요 없다.**
조종 쪽 자세한 설명(보드 프로토콜, 지연, 루트 명령)은 [auto/README.md](auto/README.md).

### 왜 일반 OCR이 아니라 글자 매칭인가
스위치 화면은 GBA 240×160을 정수배로 키운 것이라, 캡처 프레임에서 원래 픽셀을 그대로 되살릴 수 있다.
그러면 같은 글자는 늘 **비트 단위로 똑같은 그림**이 된다. 그래서 글자를 잘라 그림 → 글자 표로 찾으면
탁점(゛)·작은 글자(っ) 오독이 없고 Vision OCR보다 훨씬 빠르다. 표에 없는 글자만 Apple Vision에게 묻고,
같은 답이 두 번 나오면 표에 넣는다. 애매한 글자는 패널의 🔤 버튼(`/glyphs`)에서 직접 고른다.
처음 몇 시간은 Vision 의존이 높고, 표가 차면 거의 매칭으로만 돈다.

## 준비물

| | 사용한 것 | 메모 |
|---|---|---|
| 게임 | Nintendo Switch 1 + 독, 일본어판 포켓몬 파이어레드 | 독에서 TV 출력(1080p)을 쓴다 |
| 캡처카드 | 1080p60 USB(UVC) HDMI 캡처카드 | 맥에서 카메라처럼 잡히면 된다. 게임 화면은 OBS로 본다 |
| 맥 | Apple Silicon, RAM 24 GB | 30B 모델이 약 17 GB. 16 GB면 `--model mlx-community/Qwen3-8B-4bit`, 번역 없이 쓰려면 `--no-llm` |
| 보드 (선택) | ESP32-S3 DevKitC-1 (USB-C 두 개) | 자동 조종·맥에서 조종할 때만. 펌웨어는 `auto/frpad/` |

## 설치

```bash
brew install python@3.12 ffmpeg
git clone https://github.com/<you>/pixel-sensei.git && cd pixel-sensei
python3.12 -m venv .venv312 && source .venv312/bin/activate
pip install -r requirements.txt

python jp_text.py build            # 포켓몬·기술·도구 이름 사전 (PokeAPI에서 받음) -> data/glossary.json
python tools/fetch_sprites.py      # 배틀 화면용 포켓몬 그림 (선택) -> data/sprites/
cp data/names.example.json data/names.json   # 주인공·라이벌·닉네임을 적어 두면 번역에서 안 건드린다
```

캡처카드 장치 번호 확인: `ffmpeg -f avfoundation -list_devices true -i ""` → 그 번호를 `run.sh`의 `--dev`에 넣는다.

## 실행

```bash
./run.sh                 # 번역 패널. 처음엔 모델을 받느라 몇 분 걸린다 (약 17 GB)
./run.sh --no-llm        # 번역 없이 화면 읽기만 (자동 배틀 돌릴 때, 메모리 17 GB 절약)
./run.sh --paused        # 읽기를 멈춘 채로 시작 (O 키로 켜기)
python fr_study.py demo  # 캡처카드 없이 예문으로 패널 확인
```

`run.sh`는 30B 모델이 GPU 메모리를 더 쓰도록 `sudo sysctl iogpu.wired_limit_mb=19456`을 실행한다
(재부팅하면 초기화돼서 맥 비밀번호를 한 번 묻는다). RAM이 다르면 숫자를 바꾼다.

패널: http://127.0.0.1:8770 — OBS 옆에 띄워 둔다.

| 키 | 동작 | 키 | 동작 |
|---|---|---|---|
| O | 읽기 켜기/끄기 | V | 음성 켜기/끄기 |
| R | 다시 읽기 | N | 단어 레일 보이기/숨기기 |
| W / G | 단어 / 문법 풀이 | S | 지금 문장 저장 |
| ↑ ↓ | 지난 문장 고르기 | | |

자동 조종: [auto/README.md](auto/README.md) 참고 (`cd auto && python padd.py`).

## 폴더

```
fr_study.py      메인: 캡처 → 읽기 → 번역 → 패널 서버 (:8770)
fr_watch.py      GBA 화면 복원, 대사 상자·화살표·메뉴 찾기
glyph_ocr.py     글자 비트맵 사전 + Vision 대체 + 라벨링 페이지
jp_text.py       이름 사전, OCR 교정 후보, 한글 발음
templates.py     자주 나오는 배틀 문장 틀 (LLM 없이 바로 번역)
dialog_merge.py  여러 상자 → 한 문장 (스크롤/새 페이지 판단)
battle.py        배틀 상태 (누가 나와 있나, 타입)
livestate.py     HP·PP·메뉴 상태 → /state (auto.py용)
snaps.py         지명 팝업 읽기, 마을·관장 사진 저장 (단어장용)
place_names.py   지명 → 한국 정발명
vocab_live.py    단어 레일: 어휘 사전 + 활용형 찾기 + 외운 단어 기록
lexicon_src.txt  게임 어휘 사전 (직접 작성: 가나|품사|한자|뜻|한자음|어원 메모)
panel.html       번역 패널 화면
run.sh           실행 스크립트
auto/            보드 펌웨어, 패드 서버(조이콘·휴대폰), 자동 배틀·루트
tools/           fetch_sprites.py, bench_llm.py (모델 속도 비교)
vocab/           build_apkg.py: 단어장 → AnkiDroid 덱 (.apkg)
data/            사전·설정. 플레이하면서 쌓이는 파일은 git에서 뺀다 (.gitignore)
```

실행하면 생기는 것 (모두 `.gitignore`): `logs/`(번역 기록), `debug/`(화면 덤프), `data/progress.json`(외운 단어),
`data/glyphs.json`·`data/glyph_*`(학습한 글자 표), `data/snaps/`, `data/sprites/`.

## 성능 (MacBook Apple Silicon, RAM 24 GB, Qwen3-30B-A3B 4bit)

- 글자 읽기: 상자가 다 찍히면 바로 (모르는 글자만 Vision OCR)
- 번역: 한 문장 약 2~4초 (prefill 약 320 tok/s, decode 약 16 tok/s). 자주 나오는 배틀 문장은 틀로 즉시
- 모델 비교는 `python tools/bench_llm.py [모델 이름]`

## 저작권 · 주의

- 이 프로젝트는 Nintendo · Game Freak · The Pokémon Company와 **관계없는 개인 공부용 도구**다.
  포켓몬 관련 이름·상표는 각 권리자의 것이다.
- **게임 데이터는 들어 있지 않다.** ROM, 게임 화면, 글꼴 비트맵(`data/glyphs.json`), 포켓몬 그림, 도감 설명은
  저장소에 없고, 각자 자기 게임 화면에서 학습하거나 공개 데이터(PokeAPI)에서 받는다.
- 보드 펌웨어는 시판 유선 패드(HORIPAD S)와 같은 USB ID로 자신을 알린다. 개인 기기에서 쓰는 용도이고,
  온라인 대전 등에서의 자동 조종은 하지 않는다. 사용 책임은 각자에게 있다.
- 사용한 것: [PokeAPI](https://github.com/PokeAPI/pokeapi) 데이터와 [sprites](https://github.com/PokeAPI/sprites),
  [mlx-lm](https://github.com/ml-explore/mlx-lm), [Qwen3](https://huggingface.co/Qwen) (Apache-2.0),
  [ocrmac](https://github.com/straussmaximilian/ocrmac), fugashi + unidic-lite, pygame(SDL), genanki.

## 라이선스

코드: [MIT](LICENSE). 위 외부 데이터·모델은 각자의 라이선스를 따른다.
