"""Rule 13, the writing style (pure: no DB, no network, no clock).

Posts read like a person talking, not a slide. Two tells of machine prose are banned
outright: the colon used to introduce ("Read-across: ...") and the em dash (or the
spaced hyphen and double hyphen written in its place). A colon between digits (a time,
a ratio such as 2:1) is not prose punctuation and stays. `style_problems` is applied by
draft.drafter.check_hard_rules to every post and by swarm.cells.cell_problems to every
cell.
"""

from __future__ import annotations

import re

# A colon that is not between two digits.
_COLON = re.compile(r"(?<!\d):|:(?!\d)")
# The em dash, the en dash, the double hyphen, and a hyphen standing alone between spaces.
_DASH = re.compile(r"[—–]|--|(?<=\s)-(?=\s)")


def style_problems(text: str) -> list[str]:
    """Why this post would be discarded for its punctuation."""
    problems: list[str] = []
    if _COLON.search(text):
        problems.append("contains a colon; rewrite it as a plain sentence")
    if m := _DASH.search(text):
        problems.append(f"contains a dash ({m.group(0)!r}); use a full stop or a comma")
    return problems
