"""Join FireRed field text boxes into one utterance (everything an NPC says until the box closes).

A box shows 2 lines. Pressing A either
  - scrolls: the 2nd line moves up and one new line appears  -> new box line 1 == old box line 2
  - or clears the box for a new page                          -> both lines are new
OCR of the same line can differ by a character between boxes, so lines are compared by similarity.

    u = Utterance()
    u.add(["あら いけない", "しまったわ…"])         -> "first"
    u.add(["しまったわ…", "わたくし エリカと もうします"]) -> "scroll"
    u.text   -> "あら いけない しまったわ… わたくし エリカと もうします"
    split_sentences(u.text) -> ["あら いけない しまったわ…", "わたくし エリカと もうします"]
"""
import difflib
import re

END = "。！？!?…」』"
SIM = 0.75                       # same line if this similar (OCR may differ by a character or two)
_SENT = re.compile(r"(?<=[。！？!?…」』])\s+")


def _norm(s):
    return re.sub(r"\s+", "", s or "")


def similar(a, b):
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if a == b:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= SIM


class Utterance:
    """Lines grouped in paragraphs: a new page (box cleared) starts a paragraph, a scroll continues it.
    In the game script a page break (\\p) almost always ends a sentence, a scroll (\\l) never does."""

    def __init__(self):
        self.paras = []          # [[line, line, ...], ...]
        self.boxes = []          # raw boxes as read (for the log)
        self.last = None
        self.new = []

    @property
    def lines(self):
        return [l for p in self.paras for l in p]

    def add(self, lines):
        """-> 'first' | 'scroll' | 'page' | 'same' | 'empty'. self.new = the lines this box added."""
        lines = [l.strip() for l in lines if l and l.strip()]
        self.new = []
        if not lines:
            return "empty"
        if self.last is not None and len(lines) == len(self.last) and all(map(similar, lines, self.last)):
            return "same"                                  # the same box read again
        prev = self.paras[-1][-1] if self.paras else None
        if prev is not None and similar(lines[0], prev):
            if len(lines) == 1:
                return "same"
            how, self.new = "scroll", lines[1:]
            self.paras[-1] += self.new
        else:
            how, self.new = ("page" if self.paras else "first"), list(lines)
            self.paras.append(list(lines))
        self.boxes.append(lines)
        self.last = lines
        return how

    @property
    def text(self):
        return " ".join(self.lines)

    def sentences(self, final=False):
        """-> [(sentence, complete)]. Split only at 。！？… (a page break is not always a sentence end).
        Everything is complete except the open end (unless it ends with 。！？… or the speech is over)."""
        parts = split_sentences(self.text)
        return [(s, final or j < len(parts) - 1 or ends_sentence(s)) for j, s in enumerate(parts)]


def strip_arrow(lines, arrow):
    """The red ▼ after the last character is read as '！' or '。' (seen in every box with the arrow):
    drop that one character. A real '！' before it stays ('ジャーナル！！' -> 'ジャーナル！')."""
    lines = list(lines)
    if arrow and lines and len(lines[-1]) > 1 and lines[-1][-1] in "！。!":
        lines[-1] = lines[-1][:-1].rstrip()
    return lines


def is_junk(lines):
    """A 'box' that is just unreadable marks (□) or a single character: not a speech."""
    t = _norm("".join(lines)).replace("□", "")
    return len(t) < 2


def ends_sentence(text):
    t = (text or "").rstrip()
    return bool(t) and t[-1] in END


def split_sentences(text):
    """Split after 。！？…」』 followed by a space. Very short pieces ('あ！', 'えっ？') join the next one."""
    parts = [p for p in _SENT.split((text or "").strip()) if p]
    out = []
    carry = ""
    for p in parts:
        p = (carry + " " + p).strip() if carry else p
        if len(_norm(p)) <= 4 and p is not parts[-1]:
            carry = p
            continue
        carry = ""
        out.append(p)
    if carry:
        out.append(carry)
    return out


if __name__ == "__main__":
    tests = [
        ([["それから・・・", "よろしかったら、"], ["よろしかったら", "これも おつかいに なって！"]],
         "それから・・・ よろしかったら、 これも おつかいに なって！"),
        ([["すー… ql-", "あら いけないて"], ["あら いけない", "しまったわ…・ ようごまー"]],
         "すー… ql- あら いけないて しまったわ…・ ようごまー"),
        ([["あら いけない", "しまったわ…"], ["しまったわ…", "わたくし エリカと もうします"]],
         "あら いけない しまったわ… わたくし エリカと もうします"),
        ([["おまちどおさまでした！", "おあずかりした ポケモンは、"],
          ["おあずかりした ポケモンは、", "みんな げんきに なりましたよ！"]],
         "おまちどおさまでした！ おあずかりした ポケモンは、 みんな げんきに なりましたよ！"),
        ([["レベル50 までの ポケモンが", "いうことを ききます"], ["そして… かいりきが", "つかえます！"]],
         "レベル50 までの ポケモンが いうことを ききます そして… かいりきが つかえます！"),
        ([["あら いけない", "しまったわ…"], ["あら いけない", "しまったわ…"]], "あら いけない しまったわ…"),
    ]
    ok = True
    for boxes, want in tests:
        u = Utterance()
        hows = [u.add(b) for b in boxes]
        good = want is None or u.text == want
        ok &= good
        print("OK " if good else "BAD", hows, "|", u.text, "|", u.sentences())
    print(split_sentences("あ！ やせいの ディグダが とびだしてきた！"))
    print(split_sentences("おまちどおさまでした！ おあずかりした ポケモンは、 みんな げんきに なりましたよ！"))
    raise SystemExit(0 if ok else 1)
