"""Ranking what to write next, the pure part: an estimate for a radar topic or a catalyst
from the traits of the story (studio/traits.py) and what the account's measured pieces say
about those traits, a penalty for every recent piece on the same company, the account's
rule for how often a piece must be about a company it has never covered, and the one
topic picked at random to keep exploring. No DB, no network, no clock: the rows, the
evidence and `seed` are parameters.

The estimate never learns companies. Two topics with the same traits get the same
estimate whether or not the account has written about their companies, except that each
piece on one of its companies in the last `ranking.novelty_days` multiplies a topic's
estimate by `ranking.repeat_penalty`: a past win counts against going back, not for it.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from studio import learn as L
from studio import traits as TR


@dataclass(frozen=True)
class Estimate:
    traits: dict[str, str]  # every trait known for it, crowd and new_company included
    times: float | None  # the expected multiple of the account's median; None: no evidence
    repeats: int  # recent pieces on one of its companies
    measured: int  # scored pieces the estimate stands on
    explore: bool = False  # the topic picked at random, whatever its estimate

    @property
    def new_company(self) -> bool:
        return self.traits.get("new_company") == "yes"

    def tags(self) -> list[str]:
        """The traits as the radar page shows them, in a fixed order."""
        return [
            f"{TR.LABELS[k].split(' (')[0].lower()}: {self.traits[k]}"
            for k in TR.ARMS
            if self.traits.get(k)
        ]

    def line(self) -> str:
        """One line for a brief."""
        bits = []
        if self.times is not None:
            bits.append(f"estimate {self.times:.1f}x the account's median")
        if self.new_company:
            bits.append("a company new to the account")
        elif self.repeats:
            bits.append(f"{self.repeats} recent piece(s) on its company")
        known = [f"{k} {v}" for k, v in self.traits.items() if k != "new_company"]
        if known:
            bits.append(", ".join(known))
        return "; ".join(bits)


def estimate(
    traits: Mapping[str, str],
    pieces: Sequence[L.Measured],
    *,
    repeats: int,
    repeat_penalty: float,
    prior_sd: float,
    post_sd: float,
) -> Estimate:
    """The expected multiple of the median for a story with these traits: per trait, the
    measured pieces' mean log score for its value, shrunk towards the median by how few
    pieces stand behind it (learn.posterior), summed over the traits, times the repeat
    penalty. A trait value no scored piece has adds nothing; with no scored piece sharing
    any trait, there is no estimate (None), only the penalty."""
    pool = L.scored(pieces)
    total, used = 0.0, 0
    for arm, value in traits.items():
        if not value:
            continue
        stat = next((s for s in L.arm_stats(pool, arm) if s.value == value), None)
        if stat is None:
            continue
        mean, _ = L.posterior(stat, prior_sd=prior_sd, post_sd=post_sd)
        total += mean
        used += 1
    penalty = max(0.0, min(1.0, repeat_penalty)) ** max(0, repeats)
    times = math.exp(total) * penalty if used else None
    return Estimate(traits=dict(traits), times=times, repeats=repeats, measured=len(pool))


def repeats_of(keys: set[str], recent: Iterable[set[str]]) -> int:
    """How many of the recent pieces (each a set of company keys) share a company."""
    return sum(1 for other in recent if keys & other) if keys else 0


@dataclass(frozen=True)
class Newness:
    """The account's rule: at least `need` of every `every` pieces on a company it had not
    written about in the `ranking.novelty_days` before."""

    recent: list[bool] = field(default_factory=list)  # newest first: was each one new
    every: int = 3
    need: int = 2

    @property
    def window(self) -> list[bool]:
        return self.recent[: max(0, self.every - 1)]

    @property
    def due(self) -> bool:
        """The next piece should be on a new company: the last `every - 1` pieces leave
        fewer than `need` new ones."""
        if self.every <= 0 or self.need <= 0:
            return False
        return sum(self.window) < self.need

    def share(self, last: int = 10) -> tuple[int, int]:
        """(new, pieces) among the last `last` pieces."""
        tail = self.recent[:last]
        return sum(tail), len(tail)

    def line(self) -> str:
        new, n = self.share()
        head = (
            f"{new} of the account's last {n} pieces were on a company it had not covered "
            f"recently (its rule: at least {self.need} of every {self.every})."
            if n
            else ""
        )
        if self.due:
            return (
                head + " The next piece should be on a company the account has not "
                "written about recently."
            ).strip()
        return head


def explore_pick(candidates: Sequence[int], *, seed: str) -> int | None:
    """One candidate at random (seeded, so a page shows the same pick all day): the topic
    the account tries whatever its estimate, so the loop keeps learning about kinds of
    stories it has little evidence on."""
    if not candidates:
        return None
    return random.Random(seed).choice(sorted(candidates))


def order(estimates: Mapping[int, Estimate]) -> list[int]:
    """Ids best first: the explore pick, then by estimate (none last), new companies before
    repeats on a tie."""

    def key(i: int) -> tuple[int, float, int, int]:
        e = estimates[i]
        return (
            0 if e.explore else 1,
            -(e.times if e.times is not None else -1.0),
            0 if e.new_company else 1,
            i,
        )

    return sorted(estimates, key=key)
