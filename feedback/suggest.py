"""Turn an Analysis into conservative, human-applied suggestions. Pure; changes nothing.

Every Suggestion names the file and key a human would edit. A suggestion is emitted only
when both sides of a comparison have at least `min_posts` posts and the median KPI differs
by more than `effect_ratio` (or |rho| >= min_abs_rho for correlations). The rubric's
PROMPT_VERSION bump and any re-score are the human's call.
"""

from __future__ import annotations

from dataclasses import dataclass

from feedback.analysis import Analysis, PostRow, compare_group
from feedback.models import DIMENSIONS

KIND_RUBRIC = "rubric_weight"
KIND_PREFILTER = "prefilter_keyword"
KIND_CADENCE = "source_cadence"
KIND_FORMAT = "format"
KIND_SLOT = "slot"
KINDS = (KIND_RUBRIC, KIND_PREFILTER, KIND_CADENCE, KIND_FORMAT, KIND_SLOT)


@dataclass
class Suggestion:
    kind: str
    target: (
        str  # file + key a human would change, e.g. "config.yaml: sources[nejm].cadence_minutes"
    )
    rationale: str  # what to change and why, in one or two sentences
    evidence: str  # the numbers behind it

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown suggestion kind {self.kind!r}")


def _ratio(inside: float | None, outside: float | None) -> float | None:
    """median_in / median_out; None when either is missing or both are zero."""
    if inside is None or outside is None:
        return None
    if inside == 0 and outside == 0:
        return None
    if outside == 0:
        return float("inf")
    return inside / outside


def _direction(ratio: float, effect_ratio: float) -> str | None:
    if ratio > effect_ratio:
        return "above"
    if ratio < 1 / effect_ratio:
        return "below"
    return None


def _evidence(
    label: str, n_in: int, med_in: float | None, n_out: int, med_out: float | None, kpi: str
) -> str:
    return (
        f"{label}: n={n_in}, median {kpi}={med_in:.0f}; "
        f"everything else: n={n_out}, median {kpi}={med_out:.0f}"
    )


def _clear_effects(
    rows: list[PostRow],
    dimension: str,
    groups: list[str],
    kpi: str,
    min_posts: int,
    effect_ratio: float,
) -> list[tuple[str, str, str]]:
    """(group, 'above'|'below', evidence) for groups with a clear, well-supported effect."""
    out = []
    for g in groups:
        n_in, med_in, n_out, med_out = compare_group(rows, dimension, g, kpi)
        if n_in < min_posts or n_out < min_posts:
            continue
        ratio = _ratio(med_in, med_out)
        if ratio is None:
            continue
        direction = _direction(ratio, effect_ratio)
        if direction:
            out.append((g, direction, _evidence(g, n_in, med_in, n_out, med_out, kpi)))
    return out


def _source_suggestions(a: Analysis, min_posts: int, effect_ratio: float) -> list[Suggestion]:
    out = []
    groups = [s.group for s in a.groups.get("source", [])]
    for g, direction, ev in _clear_effects(
        a.rows, "source", groups, a.kpi, min_posts, effect_ratio
    ):
        if direction == "above":
            rationale = (
                f"Posts from source '{g}' earn far more {a.kpi} than the rest. Consider a "
                f"shorter cadence_minutes for it so its items reach the digest sooner, and a "
                f"higher audience_interest anchor for it in the rubric few-shots."
            )
        else:
            rationale = (
                f"Posts from source '{g}' earn far fewer {a.kpi} than the rest. Consider a "
                f"longer cadence_minutes (or enabled: false) for it, or a stricter prefilter "
                f"for its items."
            )
        out.append(
            Suggestion(KIND_CADENCE, f"config.yaml: sources[{g}].cadence_minutes", rationale, ev)
        )
    return out


def _topic_suggestions(a: Analysis, min_posts: int, effect_ratio: float) -> list[Suggestion]:
    out = []
    groups = [s.group for s in a.groups.get("topic", []) if s.group != "untagged"]
    for g, direction, ev in _clear_effects(a.rows, "topic", groups, a.kpi, min_posts, effect_ratio):
        if direction == "above":
            rationale = (
                f"Posts tagged '{g}' outperform. Make sure every keyword for this topic is in "
                f"prefilter.allow_keywords, and consider listing the topic under "
                f"expertise.bonus_topics so expertise_fit rewards it."
            )
        else:
            rationale = (
                f"Posts tagged '{g}' underperform. Consider removing its weakest keywords from "
                f"prefilter.allow_keywords or lowering how the rubric few-shots reward it."
            )
        out.append(
            Suggestion(
                KIND_PREFILTER, f"config.yaml: prefilter.allow_keywords ({g})", rationale, ev
            )
        )
    return out


def _evidence_suggestions(a: Analysis, min_posts: int, effect_ratio: float) -> list[Suggestion]:
    out = []
    groups = [s.group for s in a.groups.get("evidence_level", []) if s.group != "unknown"]
    for g, direction, ev in _clear_effects(
        a.rows, "evidence_level", groups, a.kpi, min_posts, effect_ratio
    ):
        verb = "reward" if direction == "above" else "discount"
        rationale = (
            f"Stories scored evidence_level='{g}' perform {direction} the rest. Consider "
            f"having compute_total {verb} that evidence level (and adjust the few-shot "
            f"examples), then bump PROMPT_VERSION and re-score."
        )
        out.append(
            Suggestion(
                KIND_RUBRIC, f"score/rubric.py: compute_total / evidence_level={g}", rationale, ev
            )
        )
    return out


def _rubric_suggestions(a: Analysis, min_posts: int, min_abs_rho: float) -> list[Suggestion]:
    out = []
    for c in a.correlations:
        if c.name not in DIMENSIONS or c.rho is None or c.n < min_posts:
            continue
        if abs(c.rho) < min_abs_rho:
            continue
        expected_negative = c.name == "hype_risk"
        positive = c.rho > 0
        if expected_negative:
            if positive:
                rationale = (
                    f"hype_risk correlates POSITIVELY with {a.kpi}: high-hype stories are "
                    f"drawing attention. The rubric subtracts hype_risk//2 from total; keep "
                    f"the penalty for editorial reasons or soften it, but do not remove it."
                )
            else:
                rationale = (
                    f"hype_risk correlates negatively with {a.kpi}, as intended. The current "
                    f"penalty (hype_risk//2) is doing its job; consider making it heavier."
                )
        elif positive:
            rationale = (
                f"'{c.name}' tracks {a.kpi} strongly. Consider weighting it above 1x in "
                f"compute_total (e.g. 1.5x) so it moves total more than the other dimensions."
            )
        else:
            rationale = (
                f"'{c.name}' is inversely related to {a.kpi}. Consider weighting it below 1x in "
                f"compute_total or revisiting its rubric definition and few-shot anchors."
            )
        out.append(
            Suggestion(
                KIND_RUBRIC,
                f"score/rubric.py: compute_total weight for {c.name}",
                rationale + " Bump PROMPT_VERSION and re-score afterwards.",
                f"Spearman rho={c.rho:+.2f} over n={c.n} posts",
            )
        )
    return out


def _format_suggestions(a: Analysis, min_posts: int, effect_ratio: float) -> list[Suggestion]:
    out = []
    for g, direction, ev in _clear_effects(
        a.rows, "kind", ["thread", "single"], a.kpi, min_posts, effect_ratio
    ):
        if direction != "above":
            continue  # the winning kind is reported once, from its own side
        rationale = (
            f"'{g}' posts earn far more {a.kpi} than the other format. Consider setting "
            f"post_format: {g} so approved drafts publish in that format by default."
        )
        out.append(Suggestion(KIND_FORMAT, "publish/config.yaml: post_format", rationale, ev))
    for g, direction, ev in _clear_effects(
        a.rows, "edited", ["edited", "unedited"], a.kpi, min_posts, effect_ratio
    ):
        if g == "edited" and direction == "above":
            rationale = (
                f"Human-edited posts earn far more {a.kpi} than unedited ones. Read the recent "
                f"original_text/edited_text pairs in the decisions table and fold the recurring "
                f"changes into the voice guide so drafts need less editing."
            )
            out.append(Suggestion(KIND_FORMAT, "draft/voice.md", rationale, ev))
        elif g == "unedited" and direction == "above":
            rationale = (
                f"Unedited posts earn far more {a.kpi} than edited ones. Check whether edits "
                f"are removing the interpretation or the hook; adjust the voice guide before "
                f"editing style."
            )
            out.append(Suggestion(KIND_FORMAT, "draft/voice.md", rationale, ev))
    return out


def _slot_suggestions(a: Analysis, min_posts: int, effect_ratio: float) -> list[Suggestion]:
    out = []
    groups = [s.group for s in a.groups.get("slot", [])]
    for g, direction, ev in _clear_effects(a.rows, "slot", groups, a.kpi, min_posts, effect_ratio):
        if g == "off-slot":
            target = "publish/config.yaml: breaking"
            what = "Breaking/off-slot posts" + (
                " outperform: keep the breaking rules broad."
                if direction == "above"
                else " underperform: narrow breaking.source_prefixes / title keywords."
            )
        else:
            target = "publish/config.yaml: slots"
            what = f"The {g} slot" + (
                f" earns far more {a.kpi}: consider moving another slot closer to it."
                if direction == "above"
                else f" earns far fewer {a.kpi}: consider moving or dropping it."
            )
        out.append(Suggestion(KIND_SLOT, target, what, ev))
    return out


def suggest(
    analysis: Analysis,
    *,
    min_posts: int = 5,
    effect_ratio: float = 2.0,
    min_abs_rho: float = 0.5,
) -> list[Suggestion]:
    """All suggestions the evidence clearly supports; an empty list is a valid answer."""
    if analysis.n_posts < min_posts:
        return []
    out: list[Suggestion] = []
    out += _rubric_suggestions(analysis, min_posts, min_abs_rho)
    out += _evidence_suggestions(analysis, min_posts, effect_ratio)
    out += _topic_suggestions(analysis, min_posts, effect_ratio)
    out += _source_suggestions(analysis, min_posts, effect_ratio)
    out += _format_suggestions(analysis, min_posts, effect_ratio)
    out += _slot_suggestions(analysis, min_posts, effect_ratio)
    return out
