"""A colon only before a list (pure).

The house style (studio/brief/voice.md, "Sentences"): a colon is written only when a list
follows it, bullets or numbered lines below it or a run of items in the same sentence
("three risks: dose, durability and price"). Anywhere else it is a full stop, a comma or a
dash. Code finds the colons that break it, so the studio's polish rounds (studio/qa.py)
and the reply checks (reply/checks.py) can send the text back.

Not counted: a colon between two digits (8:30, a 1:1 randomisation, 4:5 cards), one in a
link ("://", blocked elsewhere anyway). The inline-list test is deliberately loose (a comma,
a semicolon, "and" or "or" after the colon in the same sentence): a missed colon costs
nothing, a false one costs a polish round.
"""

from __future__ import annotations

import re

_LIST_LINE_RE = re.compile(r"^\s*(?:[•·*–—-]|\d{1,2}[.)])\s*\S")
_SENTENCE_END_RE = re.compile(r"[.!?](?=\s|$)")
_INLINE_LIST_RE = re.compile(r"[,;]|\b(?:and|or)\b|&", re.I)
SNIPPET = 40


def _list_below(lines: list[str], i: int) -> bool:
    """Whether the first non-blank line after line `i` is a bullet or a numbered item."""
    for line in lines[i + 1 :]:
        if line.strip():
            return bool(_LIST_LINE_RE.match(line))
    return False


def stray_colons(text: str) -> list[str]:
    """The colons in `text` that no list follows, each as the words around it."""
    out: list[str] = []
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        for m in re.finditer(":", line):
            k = m.start()
            before, after = line[:k], line[k + 1 :]
            if before[-1:].isdigit() and after[:1].isdigit():
                continue
            if after.startswith("//"):
                continue
            if not after.strip():
                if _list_below(lines, i):
                    continue
            else:
                end = _SENTENCE_END_RE.search(after)
                if _INLINE_LIST_RE.search(after[: end.start()] if end else after):
                    continue
            words = (before[-SNIPPET:] + ":" + after[:SNIPPET]).strip()
            out.append(words)
    return out


def colon_problems(text: str, label: str) -> list[str]:
    """One problem naming the stray colons in `text`; empty when there are none."""
    found = stray_colons(text)
    if not found:
        return []
    shown = "; ".join(f'"{w}"' for w in found[:3])
    more = f" and {len(found) - 3} more" if len(found) > 3 else ""
    return [
        f"{label} uses a colon where no list follows ({shown}{more}); write a colon only "
        "before bullets, numbered lines or a run of items, and anywhere else end the "
        "sentence or use a comma"
    ]
