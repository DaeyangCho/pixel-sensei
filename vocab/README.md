# vocab — 단어장 → AnkiDroid 덱

`build_apkg.py`는 단어장 페이지(`vocab/fr-vocab.html`, 개인 자료라 저장소에는 없음)의 데이터와
게임 어휘 사전(`../lexicon_src.txt`), 외운 단어 기록(`../data/progress.json`)으로 AnkiDroid 덱을 만든다.

```bash
pip install genanki
python vocab/build_apkg.py        # -> vocab/anki/FR_japanese_v2.apkg
```

노트 ID가 덱+단어로 고정돼 있어서, 새로 만든 덱을 다시 가져오면 기존 카드의 학습 기록은 그대로 두고
바뀐 내용과 새 카드만 들어간다.
