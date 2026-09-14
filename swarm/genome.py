"""A genome is the swarm's heritable part: the slots a thread is built from, each slot's
local rule, and the topology numbers (fan-out, layers). Pure: no DB, no network. Phase one
seeds DEFAULT_GENOME; phase three writes children of it (parent_id)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class Slot:
    name: str
    rule: str


@dataclass
class Genome:
    name: str
    slots: list[Slot]
    fan_out: int
    layers: int
    parent_id: int | None = None
    id: int | None = None
    notes: str = ""

    def slot_names(self) -> list[str]:
        return [s.name for s in self.slots]

    def to_json(self) -> str:
        d = asdict(self)
        d.pop("id", None)
        return json.dumps(d, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str, *, id: int | None = None) -> Genome:
        d = json.loads(text)
        slots = [Slot(**s) for s in d.get("slots", [])]
        if not slots:
            raise ValueError("genome has no slots")
        return cls(
            name=str(d["name"]),
            slots=slots,
            fan_out=int(d.get("fan_out", 6)),
            layers=int(d.get("layers", 2)),
            parent_id=d.get("parent_id"),
            id=id,
            notes=str(d.get("notes", "")),
        )


# Slot names the engine gives special hard rules to (cells.py): the first post must carry
# the preprint label for a preprint, the last must carry the source URL.
HOOK = "hook"
CLOSER = "closer"


DEFAULT_GENOME = Genome(
    name="default-6",
    fan_out=6,
    layers=2,
    notes="Phase-one seed: six slots in thread order, one narrow job each.",
    slots=[
        Slot(
            HOOK,
            "Open the thread. State the single most important finding or event in one "
            "sentence a buy-side reader stops for, and name the company, asset or trial. "
            "No throat-clearing, no 'thread', no emoji. Numbers only if written verbatim "
            "in the source.",
        ),
        Slot(
            "mechanism",
            "Explain the science in one post: the target, the modality (CAR-T, T-cell "
            "engager, bispecific, ADC...), the setting and the trial design as the source "
            "states them. Precise, no hype words.",
        ),
        Slot(
            "thesis",
            "Say what this does to the company's thesis: what it confirms, what it "
            "weakens, and how it compares with what the market expected or with the "
            "nearest competitor. An interpretation, never a buy/sell/hold call.",
        ),
        Slot(
            "catalyst",
            "Name what to watch next: the next readout, filing, decision date, "
            "conference or deal step, and roughly when, as far as the source supports "
            "it. If the source gives no timing, say what would need to be true.",
        ),
        Slot(
            "risk",
            "The honest caveat: sample size, follow-up, single-arm design, endpoint "
            "choice, safety signal, or what is overhyped. One post, specific, no "
            "moralising.",
        ),
        Slot(
            CLOSER,
            "Close in one sentence with the one-line takeaway and then the primary "
            "source URL verbatim on its own. If the source is a preprint, say so here "
            "too.",
        ),
    ],
)


def genome_dict(g: Genome) -> dict[str, Any]:
    return asdict(g)
