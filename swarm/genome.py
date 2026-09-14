"""A genome is the swarm's heritable part: the slots a thread is built from, each slot's
local rule, and the topology numbers (fan-out, layers). Pure: no DB, no network. Phase one
seeds DEFAULT_GENOME; phase two adds the seed population; phase three breeds children
(parent_id, `swarm/mutate.py`) and adds DESIGNER genomes: a draft.chart.Style preset the
picture starts from before the image grader turns its knobs."""

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


# Phase two needs variance to select on before phase three can breed. Two hand-written
# variants of the default: a wide, shallow one and a deep, short one. run_draft.py drafts
# the unretired seeds round-robin (swarm.store.next_genome).
WIDE_GENOME = Genome(
    name="wide-6",
    fan_out=8,
    layers=1,
    notes="Phase-two seed: same six slots, more proposals, no synthesis layer.",
    slots=list(DEFAULT_GENOME.slots),
)

DEEP_GENOME = Genome(
    name="deep-4",
    fan_out=4,
    layers=3,
    notes="Phase-two seed: four slots, two synthesis layers.",
    slots=[
        DEFAULT_GENOME.slots[0],
        Slot(
            "thesis",
            "In one post: the science as the source states it (target, modality, setting, "
            "design) and what the result does to the company's thesis. An interpretation, "
            "never a buy/sell/hold call.",
        ),
        Slot(
            "risk",
            "The honest caveat and what to watch next: sample size, follow-up, design, the "
            "next readout or decision the source supports. Specific, no moralising.",
        ),
        DEFAULT_GENOME.slots[-1],
    ],
)

SEED_GENOMES: list[Genome] = [DEFAULT_GENOME, WIDE_GENOME, DEEP_GENOME]


def genome_dict(g: Genome) -> dict[str, Any]:
    return asdict(g)


# Bounds a bred child must stay inside (swarm/mutate.py validates against these).
MIN_SLOTS, MAX_SLOTS = 3, 6
MIN_FAN_OUT, MAX_FAN_OUT = 2, 12
MIN_LAYERS, MAX_LAYERS = 1, 4


@dataclass
class Designer:
    """A designer genome: the Style knobs (draft.chart.Style field -> value) a chart or
    table is first drawn with. The grader loop still adjusts from there."""

    name: str
    style: dict[str, Any]
    parent_id: int | None = None
    id: int | None = None
    notes: str = ""

    def to_json(self) -> str:
        d = asdict(self)
        d.pop("id", None)
        return json.dumps(d, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str, *, id: int | None = None) -> Designer:
        d = json.loads(text)
        return cls(
            name=str(d["name"]),
            style=dict(d.get("style") or {}),
            parent_id=d.get("parent_id"),
            id=id,
            notes=str(d.get("notes", "")),
        )


SEED_DESIGNERS: list[Designer] = [
    Designer("house", {}, notes="Phase-three seed: the house style as shipped."),
    Designer(
        "compact",
        {"font_scale": 0.9, "row_pitch": 0.085, "table_row_height": 0.09, "bar_height": 0.55},
        notes="Phase-three seed: tighter rows, slightly smaller text.",
    ),
    Designer(
        "bold",
        {"title_scale": 1.25, "highlight_first": True, "track": False, "bar_height": 0.6},
        notes="Phase-three seed: bigger title, first bar highlighted, no tracks.",
    ),
]
