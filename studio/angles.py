"""The angle library (studio/angles.yaml) and the variety rules over it (pure apart from
reading the YAML file).

An angle is the question a piece answers. The app never decides which angle fits a
story; it decides which angles are on offer (none of the last few pieces' angles, so
the account does not repeat itself) and the session picks the one that fits, saying why.
A human may name an angle for a piece, which overrides the rotation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from studio.settings import ANGLES_PATH

SHAPES = ("long_post", "thread", "short_post")
HOOK_STYLES = (
    "juxtaposition",
    "hard_number",
    "question",
    "bold_claim",
    "stakes_and_clock",
    "contrarian",
    "story",
)


@dataclass(frozen=True)
class Angle:
    key: str
    name: str
    question: str
    signals: str = ""
    shapes: tuple[str, ...] = ()
    cards: tuple[str, ...] = ()
    reference: str = ""
    notes: str = ""

    def brief(self) -> str:
        """The angle as the session sees it in a stage prompt."""
        lines = [f"- {self.key} ({self.name}): {self.question}"]
        if self.signals:
            lines.append(f"  Fits when: {self.signals}")
        if self.shapes:
            lines.append(f"  Usual shapes: {', '.join(self.shapes)}")
        if self.cards:
            lines.append(f"  Cards that usually carry it: {', '.join(self.cards)}")
        if self.reference:
            lines.append(f"  Reference piece at this angle: {self.reference}")
        if self.notes:
            lines.append(f"  Craft: {' '.join(self.notes.split())}")
        return "\n".join(lines)


@dataclass
class AngleOffer:
    """What the write stage is allowed to choose from, and why some are missing."""

    angles: list[Angle]
    held_back: list[str] = field(default_factory=list)  # recently used, not on offer
    forced: bool = False  # a human named the angle


def _as_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in (value or ()))


def load_angles(path: str | Path | None = None) -> dict[str, Angle]:
    """The library in file order. Raises ValueError on a malformed entry."""
    with open(path or ANGLES_PATH, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict) or not raw:
        raise ValueError("studio/angles.yaml must map angle keys to entries")
    out: dict[str, Angle] = {}
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            raise ValueError(f"angle {key!r} must be a mapping")
        name = str(entry.get("name") or "").strip()
        question = " ".join(str(entry.get("question") or "").split())
        if not name or not question:
            raise ValueError(f"angle {key!r} needs a name and a question")
        shapes = _as_tuple(entry.get("shapes"))
        bad = [s for s in shapes if s not in SHAPES]
        if bad:
            raise ValueError(f"angle {key!r} has unknown shapes {bad}")
        out[str(key)] = Angle(
            key=str(key),
            name=name,
            question=question,
            signals=" ".join(str(entry.get("signals") or "").split()),
            shapes=shapes,
            cards=_as_tuple(entry.get("cards")),
            reference=str(entry.get("reference") or ""),
            notes=str(entry.get("notes") or ""),
        )
    return out


def offer(
    library: dict[str, Angle],
    recent_angles: list[str],
    *,
    avoid: int,
    requested: str = "",
) -> AngleOffer:
    """The angles the session may choose from. `recent_angles` is newest first; the newest
    `avoid` distinct angles the library knows are held back (a repeat or an unknown key
    does not use up a place, so at least the last `avoid` pieces' angles are always held
    back). A requested angle is the only one offered. If holding back would leave nothing
    (a tiny library), everything is offered."""
    if requested:
        if requested not in library:
            raise ValueError(f"unknown angle {requested!r}")
        return AngleOffer(angles=[library[requested]], forced=True)
    held: list[str] = []
    for key in recent_angles:
        if len(held) >= max(0, avoid):
            break
        if key in library and key not in held:
            held.append(key)
    angles = [a for k, a in library.items() if k not in held]
    if not angles:
        return AngleOffer(angles=list(library.values()))
    return AngleOffer(angles=angles, held_back=held)


def hooks_to_avoid(recent_hooks: list[str], avoid: int) -> list[str]:
    """The hook styles of the last `avoid` pieces (newest first), without repeats."""
    out: list[str] = []
    for h in recent_hooks[: max(0, avoid)]:
        if h and h not in out:
            out.append(h)
    return out
