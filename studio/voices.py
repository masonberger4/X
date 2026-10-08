"""The voices a studio piece is written in: the playbook's "## Voices" section (pure: no DB,
no network, no clock).

A voice is a way of sounding: the register, the rhythm, how the writer's own reactions come
through. Every voice speaks as a person in the first person (studio/brief/voice.md, "Sound
like a person"); what changes from voice to voice is how. Each piece gets one, drawn by the
app (studio/runner.py:assign_voice, studio/learn.py:draw_voice) and recorded on the piece
(studio_pieces.voice), so what X says and what the editor changed can be read by voice.

The voices live in the playbook, so they come with its history, its editor, its revert and
the learning loop's proposals (a learned change to them always waits for the editor:
studio/playbook.py:rewrite). The section looks like this:

    ## Voices
    Any words before the first voice are for the reader of the playbook.

    ### desk_note: The desk note
    How the voice sounds, in a few sentences, with a line or two of it.

A key is lower-case letters, digits and "_", and the account's numbers are kept by key: a
voice keeps its key while its wording is sharpened, and a different voice gets a new one.
A playbook with no "## Voices" heading at all (a copy from before there were voices) reads
the shipped seed's (`with_seed`); a heading with no voice under it turns voices off.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

HEADING = "## Voices"
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
MAX_VOICES = 8
MAX_WORDS = 150  # per voice
MIN_LEARNED = 2  # a learned rewrite keeps at least this many voices

_SECTION_RE = re.compile(r"^##[ \t]+Voices\b[^\n]*$", re.M)
_NEXT_SECTION_RE = re.compile(r"^#{1,2}[ \t]", re.M)  # "# x" or "## x", never "### x"
_ENTRY_RE = re.compile(r"^###[ \t]+([^\n]*)$", re.M)
_STILL_TO_LEARN_RE = re.compile(r"^##[ \t]+Still to learn\b", re.M)


@dataclass(frozen=True)
class Voice:
    key: str
    name: str
    text: str

    def brief(self) -> str:
        """The voice as a session is told it."""
        return f"{self.name} ({self.key})\n{self.text}"

    def as_dict(self) -> dict[str, str]:
        return {"key": self.key, "name": self.name, "text": self.text}

    def same(self) -> tuple[str, str, str]:
        """What makes two voices the same voice: whitespace aside, key, name and words."""
        return (self.key, " ".join(self.name.split()), " ".join(self.text.split()))


def _span(text: str) -> tuple[int, int, int] | None:
    """(start of the heading, end of the heading line, end of the section), or None."""
    m = _SECTION_RE.search(text)
    if m is None:
        return None
    nxt = _NEXT_SECTION_RE.search(text, m.end())
    return m.start(), m.end(), nxt.start() if nxt else len(text)


def has_section(text: str) -> bool:
    return _span(text) is not None


def section(text: str) -> str:
    """The Voices section as written, heading included ("" when there is none)."""
    span = _span(text)
    return text[span[0] : span[2]].strip() if span else ""


def _entries(text: str) -> list[tuple[str, str]]:
    """Each "### heading" of the section with the text under it, in order."""
    span = _span(text)
    if span is None:
        return []
    body = text[span[1] : span[2]]
    marks = list(_ENTRY_RE.finditer(body))
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        out.append((m.group(1).strip(), body[m.end() : end].strip()))
    return out


def _heading(heading: str) -> tuple[str, str]:
    """ "desk_note: The desk note" -> ("desk_note", "The desk note"); a bare key names
    itself."""
    key, sep, name = heading.partition(":")
    key = key.strip().strip("`")
    name = name.strip() if sep else ""
    return key, name or key.replace("_", " ").capitalize()


def _words(text: str) -> int:
    return len(text.split())


def parse(text: str) -> list[Voice]:
    """The voices a playbook lists, in order. Lenient: an entry with a malformed key, no
    words or a key already taken is left out (`problems` names it)."""
    out: list[Voice] = []
    seen: set[str] = set()
    for heading, body in _entries(text):
        key, name = _heading(heading)
        if not KEY_RE.match(key) or not body or key in seen:
            continue
        seen.add(key)
        out.append(Voice(key=key, name=name, text=body))
    return out


def problems(text: str) -> list[str]:
    """What is wrong with the playbook's Voices section, [] when it is fine or absent."""
    if not has_section(text):
        return []
    out: list[str] = []
    seen: set[str] = set()
    entries = _entries(text)
    for heading, body in entries:
        key, _ = _heading(heading)
        label = f'the voice "### {heading}"'
        if not KEY_RE.match(key):
            out.append(
                f"{label}: start the heading with a key of lower-case letters, digits and _ "
                'then a colon and the name ("### desk_note: The desk note")'
            )
            continue
        if key in seen:
            out.append(f"{label}: the key {key} is listed twice")
        seen.add(key)
        if not body:
            out.append(f"{label} says nothing about how it sounds")
        elif _words(body) > MAX_WORDS:
            out.append(f"{label} runs {_words(body)} words (at most {MAX_WORDS})")
    if len(entries) > MAX_VOICES:
        out.append(f"the Voices section lists {len(entries)} voices (at most {MAX_VOICES})")
    return out


def without(text: str) -> str:
    """The playbook without its Voices section: what a session reads as the playbook (it is
    told its own voice, not the others, so it never writes in a blend of them)."""
    span = _span(text)
    if span is None:
        return text
    head, tail = text[: span[0]].rstrip(), text[span[2] :].lstrip()
    if not head:
        return tail
    return head + ("\n\n" + tail if tail else "\n")


def with_seed(text: str, seed: str) -> str:
    """The playbook with the seed's Voices section added when it has none: a copy saved
    before there were voices still gets them, and its next save keeps them. The section
    goes before "## Still to learn", where the seed has it, else at the end."""
    if has_section(text) or not has_section(seed):
        return text
    voices = section(seed)
    m = _STILL_TO_LEARN_RE.search(text)
    if m is None:
        return text.rstrip() + "\n\n" + voices + "\n"
    return text[: m.start()].rstrip() + "\n\n" + voices + "\n\n" + text[m.start() :]


def given(key: str, recorded: Any) -> Voice | None:
    """The voice a piece was given, as it was worded then: `key` is the piece's voice and
    `recorded` what its meta keeps ({"key", "name", "text", ...}). None when the record is
    missing or is of another voice."""
    if not key or not isinstance(recorded, dict) or recorded.get("key") != key:
        return None
    text = str(recorded.get("text") or "").strip()
    if not text:
        return None
    return Voice(key=key, name=str(recorded.get("name") or key), text=text)


def changed(before: str, after: str) -> bool:
    """Whether two playbooks list different voices (wording aside from whitespace)."""
    return [v.same() for v in parse(before)] != [v.same() for v in parse(after)]
