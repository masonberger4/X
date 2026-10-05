"""Characters the way X counts them (pure: no I/O).

X weights text, it does not count code points: most Latin, Greek and Cyrillic text and
common punctuation count 1, everything else 2, an emoji (including a skin-tone or
ZWJ sequence, a flag or a keycap) counts 2 as a whole, and a URL counts 23 whatever its
length. This follows twitter-text's v3 configuration closely enough to keep a post under
its limit; the studio leaves headroom (studio/config.yaml `x.headroom`) for the cases it
misses.
"""

from __future__ import annotations

import re
import unicodedata

URL_WEIGHT = 23
# Code point ranges that count 1 (twitter-text v3 "ranges" with weight 100).
_LIGHT = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))
_URL = re.compile(r"https?://\S+", re.I)
_ZWJ = 0x200D
_VS16 = 0xFE0F
_KEYCAP = 0x20E3


def _is_light(cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in _LIGHT)


def _is_emoji_base(cp: int) -> bool:
    return (
        0x1F000 <= cp <= 0x1FAFF
        or 0x2600 <= cp <= 0x27BF
        or 0x2B00 <= cp <= 0x2BFF
        or 0x2300 <= cp <= 0x23FF
        or 0x1F1E6 <= cp <= 0x1F1FF
    )


def _is_modifier(cp: int) -> bool:
    return cp in (_VS16, _KEYCAP) or 0x1F3FB <= cp <= 0x1F3FF or 0xE0020 <= cp <= 0xE007F


def _is_regional(cp: int) -> bool:
    return 0x1F1E6 <= cp <= 0x1F1FF


def _starts_emoji(cps: list[int], i: int) -> bool:
    if _is_emoji_base(cps[i]):
        return True
    # A keycap (1️⃣, #️⃣) or a character asking for emoji presentation (©️) is one emoji too.
    nxt = cps[i + 1] if i + 1 < len(cps) else 0
    return nxt in (_VS16, _KEYCAP) and not _is_modifier(cps[i]) and cps[i] != _ZWJ


def x_length(text: str) -> int:
    """Weighted length of one post as X counts it."""
    text = unicodedata.normalize("NFC", text or "")
    urls = _URL.findall(text)
    body = _URL.sub("", text)
    total = URL_WEIGHT * len(urls)
    cps = [ord(c) for c in body]
    i = 0
    while i < len(cps):
        cp = cps[i]
        if _starts_emoji(cps, i):
            # One emoji: the base plus any modifiers and ZWJ-joined parts count 2 in all.
            i += 1
            if _is_regional(cp) and i < len(cps) and _is_regional(cps[i]):
                i += 1  # a flag is exactly two regional indicators; the next pair is a new flag
            while i < len(cps):
                if _is_modifier(cps[i]):
                    i += 1
                elif cps[i] == _ZWJ and i + 1 < len(cps):
                    i += 2
                else:
                    break
            total += 2
            continue
        if _is_modifier(cp):
            i += 1  # a stray variation selector adds nothing a reader sees
            continue
        total += 1 if _is_light(cp) else 2
        i += 1
    return total
