"""The lines a reply may never cross, checked in code (pure).

The same blocks the studio holds a post to (studio/safety.py: investment or medical advice,
a price target of the account's own, a link or a bare domain), plus: one post within the
X-weighted limit (studio/xcount.py), no colon that no list follows (studio/colons.py), and
no @handle that is not known. Known handles are
the ones config.yaml gives (draft/tags.py:load_handles), the KOL list's `kol.handles` and
the author of the post being answered, as the writer read it off the screenshot. Handles
are never guessed.
"""

from __future__ import annotations

import re
from typing import Any

from draft.tags import load_handles
from studio import colons, safety
from studio.xcount import x_length

LABEL = "the reply"


def known_handles(root_cfg: dict[str, Any] | None, author: str = "") -> list[str]:
    """Every handle a reply may write, as configured (the author last)."""
    cfg = root_cfg or {}
    out = [h.handle for h in load_handles(cfg)]
    for h in (cfg.get("kol") or {}).get("handles") or []:
        handle = h.get("handle") if isinstance(h, dict) else h
        handle = str(handle or "").strip().lstrip("@")
        if handle:
            out.append(handle)
    if author:
        out.append(author.lstrip("@"))
    seen: set[str] = set()
    return [h for h in out if not (h.lower() in seen or seen.add(h.lower()))]


def clean(text: str, author: str = "") -> str:
    """The reply as it will be pasted: trimmed, without the author's @handle in front
    (X's reply box puts it there already)."""
    text = (text or "").strip()
    if author:
        text = re.sub(rf"^@{re.escape(author.lstrip('@'))}\b[\s,:]*", "", text, flags=re.I)
    return text.strip()


def problems(text: str, *, allowed: list[str] | set[str], max_chars: int) -> list[str]:
    """Why this reply must not be queued; empty when it may."""
    text = (text or "").strip()
    if not text:
        return [f"{LABEL} is empty"]
    out: list[str] = []
    if "\n---\n" in text:
        out.append(f"{LABEL} is more than one post; write one")
    n = x_length(text)
    if n > max_chars:
        out.append(f"{LABEL} is {n} characters as X counts them; the limit is {max_chars}")
    out += safety.blocking_problems(text, LABEL)
    out += colons.colon_problems(text, LABEL)
    ok = {h.lower() for h in allowed}
    unknown = [h for h in safety.handles_in(text) if h.lower() not in ok]
    if unknown:
        listed = ", ".join(f"@{h}" for h in dict.fromkeys(unknown))
        out.append(f"{LABEL} uses handles that are not known ({listed}); name them in words")
    return out
